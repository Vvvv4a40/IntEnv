using System.IO;
using System.Net.Http;
using System.Windows;
using Envi.Core;
using Envi.Core.Configuration;
using Envi.Core.Logging;
using Envi.Core.Providers;
using Envi.Core.Routing;
using Envi.Core.Tools;

namespace Envi.Desktop;

public partial class App : Application
{
    private HttpClient? _httpClient;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        bool smokeTest = e.Args.Contains("--smoke-test", StringComparer.Ordinal);

        try
        {
            string settingsPath = Path.Combine(AppContext.BaseDirectory, "envi.settings.json");

            for (int index = 0; index < e.Args.Length; index++)
            {
                if (e.Args[index] == "--settings" && index + 1 < e.Args.Length)
                {
                    settingsPath = Path.GetFullPath(e.Args[++index]);
                }
                else if (e.Args[index] == "--smoke-test")
                {
                    smokeTest = true;
                }
                else
                {
                    throw new ArgumentException($"Неизвестный аргумент запуска: {e.Args[index]}");
                }
            }

            AppSettings settings = AppSettings.Load(settingsPath);
            string? apiKey = Environment.GetEnvironmentVariable("GROQ_API_KEY");
            _httpClient = new HttpClient();

            var model = new GroqClient(_httpClient, settings, apiKey);
            var tools = new ToolRegistry(settings);
            string logPath = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "Envi", "events.jsonl");
            var events = new JsonEventLog(logPath);
            var assistant = new AssistantService(model, tools, new RuleRouter(), settings, events);
            var window = new MainWindow(settings, model, assistant, tools, !string.IsNullOrWhiteSpace(apiKey));

            if (smokeTest)
            {
                // Constructs the UI and dependencies without opening a window, microphone or API connection.
                Shutdown(0);
                return;
            }

            MainWindow = window;
            window.Show();
        }
        catch (Exception exception)
        {
            if (smokeTest)
                Console.Error.WriteLine($"Не удалось запустить Envi: {exception.Message}");
            else
                MessageBox.Show($"Не удалось запустить Envi: {exception.Message}", "Envi",
                    MessageBoxButton.OK, MessageBoxImage.Error);
            Shutdown(1);
        }
    }

    protected override void OnExit(ExitEventArgs e)
    {
        _httpClient?.Dispose();
        base.OnExit(e);
    }
}
