namespace Envi.Core;

public record ToolCall(string Name, string ArgumentsJson, string Id = "");
public record ModelReply(string? Content, IReadOnlyList<ToolCall> ToolCalls, string? FinishReason = null);
public record ChatMessage(string Role, string? Content = null, string? ToolCallId = null,
    IReadOnlyList<ToolCall>? ToolCalls = null);

public interface IModelClient
{
    Task<ModelReply> CompleteAsync(IReadOnlyList<ChatMessage> messages,
        IReadOnlyList<object> tools, CancellationToken token);
}

public interface IEventSink
{
    void Record(string eventType, object? data = null);
}

/// <summary>Policy/execution boundary; tests can replace it without starting Windows applications.</summary>
public interface IToolRegistry
{
    IReadOnlyList<object> Schemas { get; }
    string? Validate(ToolCall call);
    bool RequiresConfirmation(ToolCall call);
    string Describe(ToolCall call);
    Task<string> ExecuteAsync(ToolCall call, CancellationToken cancellationToken = default);
}

public sealed class AssistantException : Exception
{
    public AssistantException(string message) : base(message) { }
    public AssistantException(string message, Exception innerException) : base(message, innerException) { }
}
