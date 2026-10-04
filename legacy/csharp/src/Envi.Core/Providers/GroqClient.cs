using System.Net;
using System.Net.Http.Headers;
using System.Text.Json;
using Envi.Core.Configuration;

namespace Envi.Core.Providers;

/// <summary>Two explicit Groq endpoints; no fallback provider, automatic retry or arbitrary base URL.</summary>
public sealed class GroqClient(HttpClient http, AppSettings settings, string? apiKey) : IModelClient
{
    private const string BaseUrl = "https://api.groq.com/openai/v1/";
    private int _sessionApiRequests;
    public int SessionApiRequests => Volatile.Read(ref _sessionApiRequests);

    public async Task<ModelReply> CompleteAsync(IReadOnlyList<ChatMessage> messages,
        IReadOnlyList<object> tools, CancellationToken token)
    {
        var body = new Dictionary<string, object?>
        {
            ["model"] = settings.ChatModel,
            ["messages"] = messages.Select(ToWireMessage).ToArray(),
            ["max_completion_tokens"] = settings.MaxOutputTokens,
            ["temperature"] = 0.2
        };
        if (tools.Count > 0)
        {
            body["tools"] = tools;
            body["tool_choice"] = "auto";
        }
        if (settings.ChatModel is "openai/gpt-oss-20b" or "openai/gpt-oss-120b")
            body["reasoning_effort"] = "low";
        using var request = new HttpRequestMessage(HttpMethod.Post, BaseUrl + "chat/completions");
        request.Content = new ByteArrayContent(JsonSerializer.SerializeToUtf8Bytes(body));
        request.Content.Headers.ContentType = new MediaTypeHeaderValue("application/json");
        var responseText = await SendAsync(request, token);
        try
        {
            using var document = JsonDocument.Parse(responseText);
            var choice = document.RootElement.GetProperty("choices")[0];
            var message = choice.GetProperty("message");
            string? content = message.TryGetProperty("content", out var text) && text.ValueKind != JsonValueKind.Null
                ? text.GetString() : null;
            var calls = new List<ToolCall>();
            if (message.TryGetProperty("tool_calls", out var toolCalls) && toolCalls.ValueKind != JsonValueKind.Null)
            {
                foreach (var call in toolCalls.EnumerateArray())
                {
                    if (call.GetProperty("type").GetString() != "function")
                        throw new AssistantException("Модель вернула неподдерживаемый тип инструмента.");
                    var function = call.GetProperty("function");
                    calls.Add(new ToolCall(function.GetProperty("name").GetString() ?? "",
                        function.GetProperty("arguments").GetString() ?? "",
                        call.GetProperty("id").GetString() ?? ""));
                }
            }
            var finishReason = choice.TryGetProperty("finish_reason", out var finish) ? finish.GetString() : null;
            return new ModelReply(content, calls, finishReason);
        }
        catch (Exception ex) when (ex is JsonException or KeyNotFoundException or InvalidOperationException or IndexOutOfRangeException)
        {
            throw new AssistantException("Groq вернул ответ неожиданного формата. Действия не выполнены.", ex);
        }
    }

    public async Task<string> TranscribeAsync(byte[] wav, CancellationToken token)
    {
        if (wav.Length < 44 || wav.Length > 44 + 32_000 * settings.RecordingMaxSeconds)
            throw new AssistantException("Запись пустая или превышает разрешённую длительность.");
        using var request = new HttpRequestMessage(HttpMethod.Post, BaseUrl + "audio/transcriptions");
        var multipart = new MultipartFormDataContent();
        var audio = new ByteArrayContent(wav);
        audio.Headers.ContentType = new MediaTypeHeaderValue("audio/wav");
        multipart.Add(audio, "file", "audio.wav");
        multipart.Add(new StringContent(settings.SttModel), "model");
        multipart.Add(new StringContent(settings.Language), "language");
        multipart.Add(new StringContent("text"), "response_format");
        multipart.Add(new StringContent("0"), "temperature");
        request.Content = multipart;
        var text = (await SendAsync(request, token)).Trim();
        if (string.IsNullOrWhiteSpace(text))
            throw new AssistantException("Речь не распознана. Попробуй записать фразу ещё раз.");
        if (text.Length > 4000)
            throw new AssistantException("Распознанный текст слишком длинный для одного запроса.");
        return text;
    }

    private async Task<string> SendAsync(HttpRequestMessage request, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        if (string.IsNullOrWhiteSpace(apiKey))
            throw new AssistantException("Нет GROQ_API_KEY. Добавь ключ в переменную окружения и перезапусти Envi. Текстовые локальные команды работают без ключа.");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", apiKey.Trim());
        ReserveRequest();
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(token);
        timeout.CancelAfter(TimeSpan.FromSeconds(settings.TimeoutSeconds));
        try
        {
            using var response = await http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, timeout.Token);
            if (!response.IsSuccessStatusCode)
            {
                var explanation = response.StatusCode switch
                {
                    HttpStatusCode.Unauthorized => "Groq не принял ключ. Проверь GROQ_API_KEY.",
                    HttpStatusCode.Forbidden => "Groq запретил запрос. Проверь доступ к модели в аккаунте.",
                    HttpStatusCode.TooManyRequests => "Достигнут лимит Groq (429). Подожди или проверь квоту в кабинете. Автоповтора нет.",
                    HttpStatusCode.BadRequest or HttpStatusCode.NotFound => "Groq отклонил запрос. Проверь выбранную модель и её доступность в кабинете.",
                    _ => $"Groq недоступен (HTTP {(int)response.StatusCode}). Повтори запрос вручную позже."
                };
                throw new AssistantException(explanation);
            }
            // Bound response memory, including chunked bodies. Never log the raw provider response.
            using var stream = await response.Content.ReadAsStreamAsync(timeout.Token);
            using var buffer = new MemoryStream();
            var chunk = new byte[8192];
            int count;
            while ((count = await stream.ReadAsync(chunk, timeout.Token)) > 0)
            {
                if (buffer.Length + count > 1_048_576)
                    throw new AssistantException("Ответ Groq превышает допустимый размер.");
                buffer.Write(chunk, 0, count);
            }
            return System.Text.Encoding.UTF8.GetString(buffer.ToArray());
        }
        catch (OperationCanceledException ex) when (!token.IsCancellationRequested)
        {
            throw new AssistantException("Groq не ответил вовремя. Автоповтора нет; попробуй вручную позже.", ex);
        }
        catch (HttpRequestException ex)
        {
            throw new AssistantException("Не удалось соединиться с Groq. Проверь интернет. Локальные текстовые команды по-прежнему доступны.", ex);
        }
    }

    private void ReserveRequest()
    {
        while (true)
        {
            var current = Volatile.Read(ref _sessionApiRequests);
            if (current >= settings.MaxSessionApiRequests)
                throw new AssistantException("Достигнут локальный лимит API-запросов этой сессии. Это счётчик запросов, не денежный бюджет. Измени настройки или начни новую сессию осознанно.");
            if (Interlocked.CompareExchange(ref _sessionApiRequests, current + 1, current) == current)
                return;
        }
    }

    private static Dictionary<string, object?> ToWireMessage(ChatMessage message)
    {
        var wire = new Dictionary<string, object?> { ["role"] = message.Role, ["content"] = message.Content };
        if (message.ToolCallId is not null) wire["tool_call_id"] = message.ToolCallId;
        if (message.ToolCalls is { Count: > 0 })
            wire["tool_calls"] = message.ToolCalls.Select(call => new
            {
                id = call.Id, type = "function", function = new { name = call.Name, arguments = call.ArgumentsJson }
            }).ToArray();
        return wire;
    }
}
