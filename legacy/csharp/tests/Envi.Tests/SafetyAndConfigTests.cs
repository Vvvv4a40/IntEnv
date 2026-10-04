using System.Net;
using System.Text;
using Envi.Core;
using Envi.Core.Configuration;
using Envi.Core.Logging;
using Envi.Core.Providers;
using Envi.Core.Routing;
using Envi.Core.Tools;

namespace Envi.Tests;

internal static class SafetyAndConfigTests
{
    public static void Register(TestRunner runner)
    {
        runner.Add("settings: load uses config directory and case-insensitive aliases", SettingsLoad);
        runner.Add("settings: invalid bounds and unknown JSON fields are rejected", SettingsRejectsInvalidValues);
        runner.Add("assistant: bounded history keeps complete user/assistant pairs", HistoryKeepsWholeTurns);
        runner.Add("assistant: truncated model tool reply executes nothing", TruncatedReplyExecutesNothing);
        runner.Add("assistant: duplicate model call IDs reject the whole batch", DuplicateCallIdsRejectBatch);
        runner.Add("assistant: declining later action prevents every action in the batch", DecliningLaterActionPreventsWholeBatch);
        runner.Add("assistant: semantically duplicate actions reject before approval", CanonicalDuplicateActionsRejectBatch);
        runner.Add("logging: event file contains metadata, not prompt, reply or API key", EventLogKeepsSecretsOut);
    }

    private static void SettingsLoad()
    {
        string directory = TemporaryDirectory();
        try
        {
            string path = Path.Combine(directory, "envi.settings.json");
            File.WriteAllText(path, """
                {"chatModel":"test-chat", "sttModel":"test-stt", "language":"ru",
                 "apps":{"MyApp":"C:\\fake\\app.exe"}, "folders":{"Project":"."}}
                """);

            AppSettings settings = AppSettings.Load(path);

            Check.Equal("test-chat", settings.ChatModel);
            Check.Equal("test-stt", settings.SttModel);
            Check.Equal("ru", settings.Language);
            Check.Equal(Path.GetFullPath(directory), settings.BaseDirectory);
            Check.True(settings.Apps.ContainsKey("myapp"));
            Check.True(settings.Folders.ContainsKey("project"));
        }
        finally { Directory.Delete(directory, recursive: true); }
    }

    private static async Task SettingsRejectsInvalidValues()
    {
        AppSettings[] invalid =
        [
            new() { TimeoutSeconds = 4 },
            new() { MaxToolRounds = 0 },
            new() { MaxOutputTokens = 127 },
            new() { HistoryMessages = 41 },
            new() { MaxSessionApiRequests = 0 },
            new() { RecordingMaxSeconds = 31 },
            new() { Language = "" },
            new() { Apps = new() { ["unsafe name"] = "app.exe" } },
            new() { Folders = new() { ["folder"] = "" } }
        ];
        foreach (AppSettings settings in invalid)
            await Check.ThrowsAsync<AssistantException>(() =>
            {
                settings.Validate();
                return Task.CompletedTask;
            });

        string directory = TemporaryDirectory();
        try
        {
            string path = Path.Combine(directory, "envi.settings.json");
            File.WriteAllText(path, "{\"ChatModel\":\"test\",\"SurprisePermission\":\"all\"}");
            await Check.ThrowsAsync<AssistantException>(() => Task.FromResult(AppSettings.Load(path)));
            File.WriteAllText(path, "{\"TimeoutSeconds\":2}");
            await Check.ThrowsAsync<AssistantException>(() => Task.FromResult(AppSettings.Load(path)));
        }
        finally { Directory.Delete(directory, recursive: true); }
    }

    private static async Task HistoryKeepsWholeTurns()
    {
        var settings = Settings();
        settings.HistoryMessages = 4;
        var model = new FakeModelClient();
        model.Enqueue(new ModelReply("answer one", []));
        model.Enqueue(new ModelReply("answer two", []));
        model.Enqueue(new ModelReply("answer three", []));
        var events = new CollectingEventSink();
        var service = new AssistantService(model, new ToolRegistry(settings), new RuleRouter(), settings, events);

        await service.ProcessAsync("question one", _ => Task.FromResult(false), CancellationToken.None);
        await service.ProcessAsync("question two", _ => Task.FromResult(false), CancellationToken.None);
        await service.ProcessAsync("question three", _ => Task.FromResult(false), CancellationToken.None);

        ChatMessage[] history = service.GetHistorySnapshot().ToArray();
        Check.Equal(4, history.Length);
        Check.Equal("user", history[0].Role);
        Check.Equal("question two", history[0].Content);
        Check.Equal("assistant", history[1].Role);
        Check.Equal("answer two", history[1].Content);
        Check.Equal("user", history[2].Role);
        Check.Equal("question three", history[2].Content);
        Check.Equal("assistant", history[3].Role);
        Check.Equal("answer three", history[3].Content);
        Check.False(history.Any(m => m.Role == "tool"));
        service.ClearHistory();
        Check.Equal(0, service.GetHistorySnapshot().Count);
        Check.True(events.Events.Any(e => e.Type == "history.cleared"));
    }

    private static async Task TruncatedReplyExecutesNothing()
    {
        var model = new FakeModelClient();
        model.Enqueue(new ModelReply("partially generated", [new ToolCall("get_time", "{}", "time-1")], "length"));
        var events = new CollectingEventSink();
        var settings = Settings();
        var service = new AssistantService(model, new ToolRegistry(settings), new RuleRouter(), settings, events);
        int approvals = 0;

        await Check.ThrowsAsync<AssistantException>(() => service.ProcessAsync("complex question", _ =>
        {
            approvals++;
            return Task.FromResult(true);
        }, CancellationToken.None));

        Check.Equal(0, approvals);
        Check.Equal(0, events.Events.Count(e => e.Type == "tool.executed"));
        Check.Equal(0, service.GetHistorySnapshot().Count);
    }

    private static async Task DuplicateCallIdsRejectBatch()
    {
        var model = new FakeModelClient();
        model.Enqueue(new ModelReply(null,
        [new ToolCall("get_time", "{}", "duplicate"), new ToolCall("get_time", "{}", "duplicate")],
        "tool_calls"));
        var events = new CollectingEventSink();
        var settings = Settings();
        var service = new AssistantService(model, new ToolRegistry(settings), new RuleRouter(), settings, events);

        await Check.ThrowsAsync<AssistantException>(() => service.ProcessAsync("complex question", _ =>
            throw new TestFailure("No approval should be requested for a rejected batch"), CancellationToken.None));

        Check.Equal(0, events.Events.Count(e => e.Type == "tool.executed"));
    }

    private static async Task DecliningLaterActionPreventsWholeBatch()
    {
        string directory = TemporaryDirectory();
        try
        {
            // An empty .exe satisfies allowlist validation but cannot start a real application.
            string fakeNotepad = Path.Combine(directory, "fake-notepad.exe");
            string fakeCalculator = Path.Combine(directory, "fake-calculator.exe");
            File.WriteAllBytes(fakeNotepad, []);
            File.WriteAllBytes(fakeCalculator, []);
            var settings = Settings();
            settings.Apps["notepad"] = fakeNotepad;
            settings.Apps["calculator"] = fakeCalculator;
            var model = new FakeModelClient();
            model.Enqueue(new ModelReply(null,
            [
                new ToolCall("open_app", "{\"app\":\"notepad\"}", "app-1"),
                new ToolCall("open_app", "{\"app\":\"calculator\"}", "app-2")
            ], "tool_calls"));
            var events = new CollectingEventSink();
            var service = new AssistantService(model, new ToolRegistry(settings), new RuleRouter(), settings, events);
            var approvals = new List<string>();

            string answer = await service.ProcessAsync("Выполни несколько действий", call =>
            {
                approvals.Add(call.ArgumentsJson);
                return Task.FromResult(call.ArgumentsJson.Contains("notepad", StringComparison.Ordinal));
            }, CancellationToken.None);

            Check.Equal("Действие отменено.", answer);
            Check.Equal(2, approvals.Count);
            Check.Contains("notepad", approvals[0]);
            Check.Contains("calculator", approvals[1]);
            Check.Equal(0, events.Events.Count(e => e.Type == "tool.executed"));
            Check.Equal(1, model.Calls.Count);
        }
        finally { Directory.Delete(directory, recursive: true); }
    }

    private static async Task CanonicalDuplicateActionsRejectBatch()
    {
        var settings = Settings();
        settings.Folders["project"] = ".";
        var model = new FakeModelClient();
        model.Enqueue(new ModelReply(null,
        [
            new ToolCall("open_folder", "{\"folder\":\"project\"}", "folder-1"),
            new ToolCall("open_folder", "{ \"folder\" : \"PROJECT\" }", "folder-2")
        ], "tool_calls"));
        var events = new CollectingEventSink();
        var service = new AssistantService(model, new ToolRegistry(settings), new RuleRouter(), settings, events);
        int approvals = 0;

        await Check.ThrowsAsync<AssistantException>(() => service.ProcessAsync("Открой папку два раза", _ =>
        {
            approvals++;
            return Task.FromResult(true);
        }, CancellationToken.None));

        Check.Equal(0, approvals);
        Check.Equal(0, events.Events.Count(e => e.Type == "tool.executed"));
    }

    private static async Task EventLogKeepsSecretsOut()
    {
        string directory = TemporaryDirectory();
        try
        {
            string logPath = Path.Combine(directory, "events.jsonl");
            const string key = "UNIT_TEST_GROQ_KEY_DO_NOT_LOG_123";
            const string prompt = "PROMPT_SECRET_DO_NOT_LOG_456";
            const string reply = "REPLY_SECRET_DO_NOT_LOG_789";
            var settings = Settings();
            var handler = new ScriptedHttpHandler();
            handler.EnqueueJson(HttpStatusCode.OK,
                "{\"choices\":[{\"finish_reason\":\"stop\",\"message\":{\"role\":\"assistant\",\"content\":\"" + reply + "\"}}]}");
            using var http = new HttpClient(handler);
            var client = new GroqClient(http, settings, key);
            var log = new JsonEventLog(logPath);
            var service = new AssistantService(client, new ToolRegistry(settings), new RuleRouter(), settings, log);

            string answer = await service.ProcessAsync(prompt, _ => Task.FromResult(false), CancellationToken.None);

            Check.Equal(reply, answer);
            Check.True(File.Exists(logPath));
            string logText = File.ReadAllText(logPath, Encoding.UTF8);
            Check.Contains("request.route", logText);
            Check.Contains("model.completed", logText);
            Check.False(logText.Contains(prompt, StringComparison.Ordinal));
            Check.False(logText.Contains(reply, StringComparison.Ordinal));
            Check.False(logText.Contains(key, StringComparison.Ordinal));
            Check.True(log.LastError is null);
            Check.Equal(1, handler.Requests.Count);
        }
        finally { Directory.Delete(directory, recursive: true); }
    }

    private static string TemporaryDirectory()
    {
        string path = Path.Combine(Path.GetTempPath(), "envi-tests-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(path);
        return path;
    }

    private static AppSettings Settings() => new() { BaseDirectory = Directory.GetCurrentDirectory() };
}
