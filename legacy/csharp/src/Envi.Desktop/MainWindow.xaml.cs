using System.ComponentModel;
using System.Windows;
using System.Windows.Input;
using Envi.Core;
using Envi.Core.Configuration;
using Envi.Core.Providers;
using Envi.Core.Tools;
using Envi.Desktop.Audio;

namespace Envi.Desktop;

public partial class MainWindow : Window
{
    private readonly AppSettings _settings;
    private readonly GroqClient _groq;
    private readonly AssistantService _assistant;
    private readonly ToolRegistry _tools;
    private readonly bool _hasApiKey;
    private readonly WaveRecorder _recorder = new();
    private CancellationTokenSource? _operationCancellation;
    private bool _busy;
    private bool _audioPending;
    private bool _recordStopping;
    private bool _audioCancelled;
    private bool _closing;
    private bool _closeReady;

    public MainWindow(AppSettings settings, GroqClient groq, AssistantService assistant,
        ToolRegistry tools, bool hasApiKey)
    {
        InitializeComponent();
        _settings = settings;
        _groq = groq;
        _assistant = assistant;
        _tools = tools;
        _hasApiKey = hasApiKey;

        SetStatus(hasApiKey
            ? "Готово. Введите запрос или запишите речь. Распознанный текст нужно проверить перед отправкой."
            : "GROQ_API_KEY не задан. Текстовые локальные команды доступны, облачные ответы и речь — нет.");
        RefreshControls();
        InputBox.Focus();
    }

    private void RefreshControls()
    {
        ApiCountText.Text = $"API-запросов в сессии: {_groq.SessionApiRequests}/{_settings.MaxSessionApiRequests}";
        bool recording = _recorder.IsRecording;
        RecordButton.Content = recording ? "Остановить запись" : "Записать речь";
        RecordButton.IsEnabled = _hasApiKey && !_busy &&
            (recording ? !_recordStopping : !_audioPending);
        SendButton.IsEnabled = !_busy && !_audioPending;
        InputBox.IsReadOnly = _busy || _audioPending;
        CancelButton.IsEnabled = _busy || _audioPending;
        ClearButton.IsEnabled = !_busy && !_audioPending;
    }

    private void SetStatus(string status) => StatusText.Text = status;

    private void AddHistory(string speaker, string message)
    {
        HistoryBox.AppendText($"{DateTime.Now:HH:mm}  {speaker}: {message}{Environment.NewLine}{Environment.NewLine}");
        if (HistoryBox.Text.Length > 100_000)
        {
            string history = HistoryBox.Text;
            int boundary = history.IndexOf(Environment.NewLine, history.Length - 80_000,
                StringComparison.Ordinal);
            int start = boundary >= 0 ? boundary + Environment.NewLine.Length : history.Length - 80_000;
            HistoryBox.Text = "…предыдущие сообщения скрыты…" + Environment.NewLine +
                Environment.NewLine + history[start..];
        }
        HistoryBox.ScrollToEnd();
    }

    private void RecordButton_Click(object sender, RoutedEventArgs e)
    {
        if (_recorder.IsRecording)
        {
            _recordStopping = true;
            SetStatus("Останавливаю запись…");
            RefreshControls();
            _ = _recorder.StopAsync(); // HandleRecordingAsync observes the same completion task.
            return;
        }

        try
        {
            _recorder.Start(_settings.RecordingMaxSeconds);
            _audioPending = true;
            _audioCancelled = false;
            _recordStopping = false;
            SetStatus($"Идёт запись (не более {_settings.RecordingMaxSeconds} с). Нажмите кнопку ещё раз для остановки.");
            RefreshControls();
            _ = HandleRecordingAsync(_recorder.Completion);
        }
        catch (Exception exception)
        {
            SetStatus($"Микрофон недоступен: {exception.Message}");
            RefreshControls();
        }
    }

    private async Task HandleRecordingAsync(Task<byte[]> completion)
    {
        try
        {
            byte[] wav = await completion;
            if (_closing || _audioCancelled)
                return;

            if (wav.Length <= 44)
            {
                SetStatus("Запись пуста. Попробуйте ещё раз.");
                return;
            }

            _busy = true;
            _operationCancellation = new CancellationTokenSource();
            SetStatus("Распознаю речь через Groq…");
            RefreshControls();
            string transcript = await _groq.TranscribeAsync(wav, _operationCancellation.Token);
            if (_closing || _audioCancelled)
                return;

            if (string.IsNullOrWhiteSpace(transcript))
            {
                SetStatus("Речь не распознана. Попробуйте ещё раз.");
                return;
            }

            // STT must not automatically execute a computer command.
            InputBox.Text = string.IsNullOrWhiteSpace(InputBox.Text)
                ? transcript.Trim()
                : $"{InputBox.Text.TrimEnd()} {transcript.Trim()}";
            InputBox.CaretIndex = InputBox.Text.Length;
            SetStatus("Речь распознана. Проверьте текст и нажмите «Отправить».");
            InputBox.Focus();
        }
        catch (OperationCanceledException)
        {
            if (!_closing)
                SetStatus("Запись или распознавание отменены.");
        }
        catch (Exception exception)
        {
            if (!_closing)
                SetStatus($"Не удалось обработать речь: {exception.Message}");
        }
        finally
        {
            _operationCancellation?.Dispose();
            _operationCancellation = null;
            _audioPending = false;
            _recordStopping = false;
            _busy = false;
            if (!_closing)
                RefreshControls();
        }
    }

    private async void SendButton_Click(object sender, RoutedEventArgs e) => await SendAsync();

    private async void InputBox_PreviewKeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Enter && Keyboard.Modifiers.HasFlag(ModifierKeys.Control))
        {
            e.Handled = true;
            await SendAsync();
        }
    }

    private async Task SendAsync()
    {
        if (_busy || _audioPending)
            return;

        string request = InputBox.Text.Trim();
        if (request.Length == 0)
            return;

        InputBox.Clear();
        AddHistory("Вы", request);
        _busy = true;
        _operationCancellation = new CancellationTokenSource();
        SetStatus("Обрабатываю запрос…");
        RefreshControls();

        try
        {
            string answer = await _assistant.ProcessAsync(request, ConfirmToolAsync,
                _operationCancellation.Token);
            if (!_closing)
            {
                AddHistory("Envi", answer);
                SetStatus("Готово.");
            }
        }
        catch (OperationCanceledException)
        {
            if (!_closing)
                SetStatus("Запрос отменён.");
        }
        catch (AssistantException exception)
        {
            if (!_closing)
            {
                AddHistory("Envi", exception.Message);
                SetStatus("Запрос не выполнен.");
            }
        }
        catch (Exception exception)
        {
            if (!_closing)
                SetStatus($"Ошибка обработки запроса: {exception.Message}");
        }
        finally
        {
            _operationCancellation?.Dispose();
            _operationCancellation = null;
            _busy = false;
            if (!_closing)
            {
                RefreshControls();
                InputBox.Focus();
            }
        }
    }

    private Task<bool> ConfirmToolAsync(ToolCall call)
    {
        return Dispatcher.InvokeAsync(() =>
        {
            if (_closing)
                return false;

            // Describe revalidates the configured target at the moment of confirmation.
            string target = _tools.Describe(call);
            return MessageBox.Show(this,
                $"Разрешить Envi выполнить действие?{Environment.NewLine}{Environment.NewLine}" +
                $"{target}{Environment.NewLine}{Environment.NewLine}" +
                $"Инструмент: {call.Name}",
                "Подтверждение действия", MessageBoxButton.YesNo,
                MessageBoxImage.Question, MessageBoxResult.No) == MessageBoxResult.Yes;
        }).Task;
    }

    private async void CancelButton_Click(object sender, RoutedEventArgs e)
    {
        _audioCancelled = true;
        _operationCancellation?.Cancel();
        if (_recorder.IsRecording)
        {
            _recordStopping = true;
            RefreshControls();
            try
            {
                await _recorder.CancelAsync();
            }
            catch (Exception exception)
            {
                if (!_closing)
                    SetStatus($"Не удалось остановить микрофон: {exception.Message}");
            }
        }
    }

    private void ClearButton_Click(object sender, RoutedEventArgs e)
    {
        _assistant.ClearHistory();
        HistoryBox.Clear();
        SetStatus("История текущего диалога очищена. Журнал событий на диске не удалялся.");
    }

    protected override async void OnClosing(CancelEventArgs e)
    {
        if (_closeReady)
        {
            base.OnClosing(e);
            return;
        }

        e.Cancel = true;
        _closing = true;
        _audioCancelled = true;
        _operationCancellation?.Cancel();
        try
        {
            await _recorder.CancelAsync();
            _recorder.Dispose();
        }
        catch
        {
            // Native cleanup already attempted; do not block window closure after an audio-driver error.
        }
        finally
        {
            _closeReady = true;
            Close();
        }
    }
}
