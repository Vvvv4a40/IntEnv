using Envi.Core;

namespace Envi.Tests;

internal sealed class FakeModelClient : IModelClient
{
    private readonly Queue<Func<ModelReply>> _replies = new();

    public List<IReadOnlyList<ChatMessage>> Calls { get; } = [];
    public List<IReadOnlyList<object>> Schemas { get; } = [];

    public void Enqueue(ModelReply reply) => _replies.Enqueue(() => reply);
    public void EnqueueFailure(Exception failure) => _replies.Enqueue(() => throw failure);

    public Task<ModelReply> CompleteAsync(
        IReadOnlyList<ChatMessage> messages,
        IReadOnlyList<object> tools,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        Calls.Add(messages.ToArray());
        Schemas.Add(tools.ToArray());
        if (_replies.Count == 0)
            throw new TestFailure("Unexpected model call without a scripted reply.");
        return Task.FromResult(_replies.Dequeue()());
    }
}

internal sealed class CollectingEventSink : IEventSink
{
    public List<(string Type, object? Data)> Events { get; } = [];

    public void Record(string eventType, object? data = null) => Events.Add((eventType, data));
}

/// <summary>Records an approved action without creating a process or touching the machine.</summary>
internal sealed class FakeToolRegistry : IToolRegistry
{
    public const string FakeResult = "FAKE_APP_OPENED_ONCE";
    public IReadOnlyList<object> Schemas { get; } = [];
    public List<ToolCall> Executed { get; } = [];
    public Action? OnExecute { get; set; }

    public string? Validate(ToolCall call) =>
        call.Name == "open_app" && call.ArgumentsJson == "{\"app\":\"notepad\"}"
            ? null : "Fake registry rejects this call.";

    public bool RequiresConfirmation(ToolCall call) => true;

    public string Describe(ToolCall call) => "Fake notepad target";

    public Task<string> ExecuteAsync(ToolCall call, CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (Validate(call) is { } error)
            throw new ArgumentException(error, nameof(call));
        Executed.Add(call);
        OnExecute?.Invoke();
        return Task.FromResult(FakeResult);
    }
}
