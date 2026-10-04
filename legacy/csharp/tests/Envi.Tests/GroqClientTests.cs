using System.Net;
using System.Text;
using System.Text.Json;
using Envi.Core;
using Envi.Core.Configuration;
using Envi.Core.Providers;
using Envi.Core.Routing;
using Envi.Core.Tools;

namespace Envi.Tests;

internal static class GroqClientTests
{
    public static void Register(TestRunner runner)
    {
        runner.Add("groq: STT sends configured Russian multipart request", RussianTranscriptionRequest);
        runner.Add("groq: chat sends roles, tool schemas and limits, parses tool calls", ChatWireContract);
        runner.Add("groq: HTTP 429 is surfaced without an automatic retry", RateLimitDoesNotRetry);
        runner.Add("groq: STT and chat share one local request quota", SharedRequestQuota);
        runner.Add("groq: local time works without API key or HTTP", MissingKeyDoesNotBlockLocalCommand);
    }

    private static async Task RussianTranscriptionRequest()
    {
        var settings = Settings();
        settings.SttModel = "test-whisper";
        settings.Language = "ru";
        var handler = new ScriptedHttpHandler();
        handler.Enqueue(_ => new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent("  привет, мир  ", Encoding.UTF8, "text/plain")
        });
        using var http = new HttpClient(handler);
        var client = new GroqClient(http, settings, "unit-test-key");

        string transcript = await client.TranscribeAsync(new byte[44], CancellationToken.None);

        Check.Equal("привет, мир", transcript);
        Check.Equal(1, handler.Requests.Count);
        Check.Equal(1, client.SessionApiRequests);
        CapturedRequest request = handler.Requests[0];
        Check.Equal(HttpMethod.Post, request.Method);
        Check.Contains("/audio/transcriptions", request.Uri?.AbsolutePath);
        Check.Equal("api.groq.com", request.Uri?.Host);
        Check.Equal("Bearer unit-test-key", request.Authorization);
        Check.Contains("multipart/form-data", request.ContentType);
        Check.Contains("name=file", request.Body);
        Check.Contains("audio.wav", request.Body);
        Check.Contains("test-whisper", request.Body);
        Check.Contains("name=language", request.Body);
        Check.Contains("ru", request.Body);
        Check.Contains("name=response_format", request.Body);
        Check.Contains("text", request.Body);
    }

    private static async Task ChatWireContract()
    {
        var settings = Settings();
        settings.ChatModel = "test-chat-model";
        settings.MaxOutputTokens = 321;
        var handler = new ScriptedHttpHandler();
        handler.EnqueueJson(HttpStatusCode.OK, """
            {"choices":[{"finish_reason":"tool_calls","message":{"role":"assistant","content":null,
            "tool_calls":[{"id":"call-9","type":"function","function":{"name":"get_time","arguments":"{}"}}]}}]}
            """);
        using var http = new HttpClient(handler);
        var client = new GroqClient(http, settings, "unit-test-key");
        object schema = new Dictionary<string, object> { ["type"] = "function", ["function"] = new { name = "get_time" } };
        ChatMessage[] messages =
        [
            new("system", "system prompt"),
            new("user", "tell me time"),
            new("assistant", ToolCalls: [new ToolCall("get_time", "{}", "old-1")]),
            new("tool", "Сейчас 12:00", "old-1")
        ];

        ModelReply reply = await client.CompleteAsync(messages, [schema], CancellationToken.None);

        Check.Equal("tool_calls", reply.FinishReason);
        Check.Equal(1, reply.ToolCalls.Count);
        Check.Equal("get_time", reply.ToolCalls[0].Name);
        Check.Equal("{}", reply.ToolCalls[0].ArgumentsJson);
        Check.Equal("call-9", reply.ToolCalls[0].Id);
        Check.Equal(1, handler.Requests.Count);
        CapturedRequest request = handler.Requests[0];
        Check.Equal(HttpMethod.Post, request.Method);
        Check.Contains("/chat/completions", request.Uri?.AbsolutePath);
        Check.Equal("Bearer unit-test-key", request.Authorization);
        using JsonDocument document = JsonDocument.Parse(request.Body);
        JsonElement root = document.RootElement;
        Check.Equal("test-chat-model", root.GetProperty("model").GetString());
        Check.Equal(321, root.GetProperty("max_completion_tokens").GetInt32());
        Check.Equal("auto", root.GetProperty("tool_choice").GetString());
        Check.Equal(1, root.GetProperty("tools").GetArrayLength());
        JsonElement[] wireMessages = root.GetProperty("messages").EnumerateArray().ToArray();
        Check.Equal(4, wireMessages.Length);
        Check.Equal("system", wireMessages[0].GetProperty("role").GetString());
        Check.Equal("user", wireMessages[1].GetProperty("role").GetString());
        Check.Equal("assistant", wireMessages[2].GetProperty("role").GetString());
        Check.Equal("old-1", wireMessages[2].GetProperty("tool_calls")[0].GetProperty("id").GetString());
        Check.Equal("tool", wireMessages[3].GetProperty("role").GetString());
        Check.Equal("old-1", wireMessages[3].GetProperty("tool_call_id").GetString());
    }

    private static async Task RateLimitDoesNotRetry()
    {
        var handler = new ScriptedHttpHandler();
        handler.EnqueueJson(HttpStatusCode.TooManyRequests, "{\"error\":{\"message\":\"quota\"}}");
        using var http = new HttpClient(handler);
        var client = new GroqClient(http, Settings(), "unit-test-key");

        AssistantException error = await Check.ThrowsAsync<AssistantException>(() => client.CompleteAsync(
            [new ChatMessage("user", "hello")], [], CancellationToken.None));

        Check.Contains("429", error.Message);
        Check.Equal(1, handler.Requests.Count);
        Check.Equal(1, client.SessionApiRequests);
    }

    private static async Task SharedRequestQuota()
    {
        var settings = Settings();
        settings.MaxSessionApiRequests = 1;
        var handler = new ScriptedHttpHandler();
        handler.Enqueue(_ => new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent("привет", Encoding.UTF8, "text/plain")
        });
        using var http = new HttpClient(handler);
        var client = new GroqClient(http, settings, "unit-test-key");

        Check.Equal("привет", await client.TranscribeAsync(new byte[44], CancellationToken.None));
        AssistantException error = await Check.ThrowsAsync<AssistantException>(() => client.CompleteAsync(
            [new ChatMessage("user", "hello")], [], CancellationToken.None));

        Check.Contains("лимит", error.Message);
        Check.Equal(1, handler.Requests.Count);
        Check.Equal(1, client.SessionApiRequests);
    }

    private static async Task MissingKeyDoesNotBlockLocalCommand()
    {
        var settings = Settings();
        var handler = new ScriptedHttpHandler();
        using var http = new HttpClient(handler);
        var client = new GroqClient(http, settings, null);
        var service = new AssistantService(client, new ToolRegistry(settings), new RuleRouter(), settings,
            new CollectingEventSink());

        string answer = await service.ProcessAsync("Который час?", _ =>
            throw new TestFailure("Local get_time must not require approval"), CancellationToken.None);

        Check.Contains("Сейчас", answer);
        Check.Equal(0, handler.Requests.Count);
        Check.Equal(0, client.SessionApiRequests);
    }

    private static AppSettings Settings() => new() { BaseDirectory = Directory.GetCurrentDirectory() };
}
