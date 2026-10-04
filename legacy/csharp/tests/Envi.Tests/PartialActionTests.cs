using Envi.Core;
using Envi.Core.Configuration;
using Envi.Core.Routing;

namespace Envi.Tests;

internal static class PartialActionTests
{
    public static void Register(TestRunner runner)
    {
        runner.Add("assistant: provider failure after approved action reports exactly what completed", FailureAfterActionIsAcknowledged);
        runner.Add("assistant: cancellation after approved action reports non-rollback", CancellationAfterActionIsAcknowledged);
        runner.Add("assistant: successful answer preserves deterministic completed-action summary", SuccessfulAnswerIncludesCompletedAction);
    }

    private static async Task FailureAfterActionIsAcknowledged()
    {
        var model = new FakeModelClient();
        model.Enqueue(OpenNotepadReply());
        model.EnqueueFailure(new AssistantException("Groq test failure"));
        var tools = new FakeToolRegistry();
        var events = new CollectingEventSink();
        var service = Service(model, tools, events);
        int approvals = 0;

        string answer = await service.ProcessAsync("Do an action then answer", _ =>
        {
            approvals++;
            return Task.FromResult(true);
        }, CancellationToken.None);

        Check.Contains("Не удалось завершить запрос", answer);
        Check.Contains("Groq test failure", answer);
        AssertCompletedExactlyOnce(answer);
        Check.Equal(1, approvals);
        Check.Equal(1, tools.Executed.Count);
        Check.Equal(1, events.Events.Count(e => e.Type == "tool.executed"));
        Check.Equal(2, model.Calls.Count);
        AssertHistory(service, "Do an action then answer", answer);
    }

    private static async Task CancellationAfterActionIsAcknowledged()
    {
        using var source = new CancellationTokenSource();
        var model = new FakeModelClient();
        model.Enqueue(OpenNotepadReply());
        var tools = new FakeToolRegistry { OnExecute = source.Cancel };
        var service = Service(model, tools, new CollectingEventSink());

        string answer = await service.ProcessAsync("Do an action then cancel", _ => Task.FromResult(true), source.Token);

        Check.Contains("Запрос отменён", answer);
        Check.Contains("не откатываются", answer);
        AssertCompletedExactlyOnce(answer);
        Check.Equal(1, tools.Executed.Count);
        Check.Equal(1, model.Calls.Count);
        AssertHistory(service, "Do an action then cancel", answer);
    }

    private static async Task SuccessfulAnswerIncludesCompletedAction()
    {
        var model = new FakeModelClient();
        model.Enqueue(OpenNotepadReply());
        model.Enqueue(new ModelReply("Задача завершена.", [], "stop"));
        var tools = new FakeToolRegistry();
        var service = Service(model, tools, new CollectingEventSink());
        int approvals = 0;

        string answer = await service.ProcessAsync("Do an action successfully", _ =>
        {
            approvals++;
            return Task.FromResult(true);
        }, CancellationToken.None);

        Check.True(answer.StartsWith("Задача завершена.\nУже выполнено:\n• ", StringComparison.Ordinal),
            "The final answer must deterministically include completed actions, not just the model's claim.");
        AssertCompletedExactlyOnce(answer);
        Check.Equal(1, approvals);
        Check.Equal(1, tools.Executed.Count);
        Check.Equal(2, model.Calls.Count);
        AssertHistory(service, "Do an action successfully", answer);
    }

    private static void AssertCompletedExactlyOnce(string answer)
    {
        Check.Contains("Уже выполнено", answer);
        Check.Contains(FakeToolRegistry.FakeResult, answer);
        Check.Equal(1, answer.Split(FakeToolRegistry.FakeResult, StringSplitOptions.None).Length - 1);
    }

    private static void AssertHistory(AssistantService service, string userText, string answer)
    {
        ChatMessage[] history = service.GetHistorySnapshot().ToArray();
        Check.Equal(2, history.Length);
        Check.Equal("user", history[0].Role);
        Check.Equal(userText, history[0].Content);
        Check.Equal("assistant", history[1].Role);
        Check.Equal(answer, history[1].Content);
    }

    private static ModelReply OpenNotepadReply() => new(null,
        [new ToolCall("open_app", "{\"app\":\"notepad\"}", "fake-action-1")], "tool_calls");

    private static AssistantService Service(FakeModelClient model, FakeToolRegistry tools, CollectingEventSink events) =>
        new(model, tools, new RuleRouter(), new AppSettings(), events);
}
