using Envi.Core;
using Envi.Core.Configuration;
using Envi.Core.Routing;
using Envi.Core.Tools;

namespace Envi.Tests;

internal static class AssistantTests
{
    public static void Register(TestRunner runner)
    {
        runner.Add("assistant: local time command never calls cloud", LocalTimeSkipsModel);
        runner.Add("assistant: declined app opens nothing and gives target to approval callback", DeclinedAppNeverExecutes);
        runner.Add("assistant: model tool result is paired with assistant call ID", ModelToolRoundTrip);
        runner.Add("assistant: unknown model tool is rejected", UnknownToolRejected);
        runner.Add("assistant: entire model tool batch is validated before any execution", MixedBatchRejectedAtomically);
        runner.Add("assistant: max tool rounds stops another action", MaxToolRoundsStops);
        runner.Add("assistant: cancelled request is not sent to model", CancellationStops);
    }

    private static async Task LocalTimeSkipsModel()
    {
        var model = new FakeModelClient();
        var service = Service(model, Settings());
        string answer = await service.ProcessAsync("Который час?", _ => Task.FromResult(false), CancellationToken.None);
        Check.Contains("Сейчас", answer);
        Check.Equal(0, model.Calls.Count);
    }

    private static async Task DeclinedAppNeverExecutes()
    {
        // This is deliberately not an executable. Even a broken approval gate cannot start a real app.
        string fakeExe = Path.Combine(Path.GetTempPath(), "envi-tests-" + Guid.NewGuid().ToString("N") + ".exe");
        File.WriteAllBytes(fakeExe, []);
        try
        {
            var settings = Settings();
            settings.Apps["notepad"] = fakeExe;
            var model = new FakeModelClient();
            var service = Service(model, settings);
            ToolCall? requested = null;
            int approvals = 0;
            string answer = await service.ProcessAsync("Открой блокнот", call =>
            {
                approvals++;
                requested = call;
                return Task.FromResult(false);
            }, CancellationToken.None);

            Check.Equal("Действие отменено.", answer);
            Check.Equal(1, approvals);
            Check.Equal("open_app", requested?.Name);
            Check.Contains("notepad", requested?.ArgumentsJson);
            Check.Equal(0, model.Calls.Count);
        }
        finally
        {
            File.Delete(fakeExe);
        }
    }

    private static async Task ModelToolRoundTrip()
    {
        var settings = Settings();
        var model = new FakeModelClient();
        model.Enqueue(new ModelReply(null, [new ToolCall("get_time", "{}", "call-7")], "tool_calls"));
        model.Enqueue(new ModelReply("Готово, время узнал.", [], "stop"));
        var service = Service(model, settings);

        string answer = await service.ProcessAsync("Назови мне точное время прямо сейчас в ответе модели", _ =>
            throw new TestFailure("get_time must not require approval"), CancellationToken.None);

        Check.Equal("Готово, время узнал.", answer);
        Check.Equal(2, model.Calls.Count);
        Check.True(model.Schemas[0].Count > 0, "Model should receive tool schemas.");
        IReadOnlyList<ChatMessage> secondRequest = model.Calls[1];
        Check.True(secondRequest.Any(m => m.Role == "assistant" && m.ToolCalls?.Any(c => c.Id == "call-7") == true));
        Check.True(secondRequest.Any(m => m.Role == "tool" && m.ToolCallId == "call-7" && m.Content?.Contains("Сейчас") == true));
    }

    private static async Task UnknownToolRejected()
    {
        var model = new FakeModelClient();
        model.Enqueue(new ModelReply(null, [new ToolCall("run_powershell", "{\"command\":\"echo hi\"}", "evil-1")], "tool_calls"));
        var service = Service(model, Settings());
        int approvals = 0;
        await Check.ThrowsAsync<AssistantException>(() => service.ProcessAsync("Выполни сложную задачу", _ =>
        {
            approvals++;
            return Task.FromResult(true);
        }, CancellationToken.None));
        Check.Equal(0, approvals);
        Check.Equal(1, model.Calls.Count);
    }

    private static async Task MixedBatchRejectedAtomically()
    {
        var model = new FakeModelClient();
        model.Enqueue(new ModelReply(null,
        [
            new ToolCall("get_time", "{}", "safe-1"),
            new ToolCall("run_powershell", "{\"command\":\"shutdown\"}", "evil-2")
        ], "tool_calls"));
        var events = new CollectingEventSink();
        var settings = Settings();
        var service = new AssistantService(model, new ToolRegistry(settings), new RuleRouter(), settings, events);
        int approvals = 0;
        await Check.ThrowsAsync<AssistantException>(() => service.ProcessAsync("Сделай всё это", _ =>
        {
            approvals++;
            return Task.FromResult(true);
        }, CancellationToken.None));
        Check.Equal(0, approvals);
        Check.Equal(0, events.Events.Count(e => e.Type == "tool.executed"),
            "A valid first call must not execute before a later invalid call in the same batch is rejected.");
    }

    private static async Task MaxToolRoundsStops()
    {
        var settings = Settings();
        settings.MaxToolRounds = 1;
        var model = new FakeModelClient();
        model.Enqueue(new ModelReply(null, [new ToolCall("get_time", "{}", "time-1")], "tool_calls"));
        model.Enqueue(new ModelReply(null, [new ToolCall("get_time", "{}", "time-2")], "tool_calls"));
        var events = new CollectingEventSink();
        var service = new AssistantService(model, new ToolRegistry(settings), new RuleRouter(), settings, events);
        await Check.ThrowsAsync<AssistantException>(() => service.ProcessAsync("Дай сложный ответ по времени", _ =>
            throw new TestFailure("get_time must not require approval"), CancellationToken.None));
        Check.Equal(2, model.Calls.Count);
        Check.Equal(1, events.Events.Count(e => e.Type == "tool.executed"));
    }

    private static async Task CancellationStops()
    {
        using var source = new CancellationTokenSource();
        source.Cancel();
        var model = new FakeModelClient();
        var service = Service(model, Settings());
        await Check.ThrowsAsync<OperationCanceledException>(() => service.ProcessAsync("Задача для облака", _ =>
            Task.FromResult(true), source.Token));
        Check.Equal(0, model.Calls.Count);
    }

    private static AssistantService Service(FakeModelClient model, AppSettings settings) =>
        new(model, new ToolRegistry(settings), new RuleRouter(), settings, new CollectingEventSink());

    private static AppSettings Settings() => new() { BaseDirectory = Directory.GetCurrentDirectory() };
}
