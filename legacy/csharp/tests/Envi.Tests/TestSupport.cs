using System.Net;
using System.Text;

namespace Envi.Tests;

internal static class Check
{
    public static void True(bool condition, string? message = null)
    {
        if (!condition) throw new TestFailure(message ?? "Expected true.");
    }

    public static void False(bool condition, string? message = null) => True(!condition, message ?? "Expected false.");

    public static void Equal<T>(T expected, T actual, string? message = null)
    {
        if (!EqualityComparer<T>.Default.Equals(expected, actual))
            throw new TestFailure(message ?? $"Expected <{expected}> but got <{actual}>.");
    }

    public static void Contains(string expectedPart, string? actual, string? message = null)
    {
        if (actual is null || !actual.Contains(expectedPart, StringComparison.OrdinalIgnoreCase))
            throw new TestFailure(message ?? $"Expected <{actual}> to contain <{expectedPart}>.");
    }

    public static async Task<T> ThrowsAsync<T>(Func<Task> action) where T : Exception
    {
        try
        {
            await action();
        }
        catch (T ex)
        {
            return ex;
        }
        catch (Exception ex)
        {
            throw new TestFailure($"Expected {typeof(T).Name}, got {ex.GetType().Name}: {ex.Message}");
        }

        throw new TestFailure($"Expected {typeof(T).Name}; no exception was thrown.");
    }
}

internal sealed class TestFailure(string message) : Exception(message);

internal sealed class TestRunner
{
    private readonly List<(string Name, Func<Task> Run)> _cases = [];

    public void Add(string name, Action action) => _cases.Add((name, () =>
    {
        action();
        return Task.CompletedTask;
    }));

    public void Add(string name, Func<Task> action) => _cases.Add((name, action));

    public async Task<int> RunAsync(string[] args)
    {
        if (args.Length > 2 || args.Length == 2 && args[0] != "--case" || args.Length == 1)
        {
            Console.Error.WriteLine("Usage: dotnet run --project tests/Envi.Tests -- [--case substring]");
            return 2;
        }

        string? filter = args.Length == 2 ? args[1] : null;
        var selected = _cases.Where(c => filter is null || c.Name.Contains(filter, StringComparison.OrdinalIgnoreCase)).ToList();
        if (selected.Count == 0)
        {
            Console.Error.WriteLine($"No tests match '{filter}'.");
            return 2;
        }

        var failed = 0;
        foreach (var (name, run) in selected)
        {
            try
            {
                await run();
                Console.WriteLine($"PASS {name}");
            }
            catch (Exception ex)
            {
                failed++;
                Console.Error.WriteLine($"FAIL {name}: {ex}");
            }
        }

        Console.WriteLine($"{selected.Count - failed}/{selected.Count} passed");
        return failed == 0 ? 0 : 1;
    }
}

internal sealed record CapturedRequest(
    HttpMethod Method,
    Uri? Uri,
    string? Authorization,
    string? ContentType,
    string Body);

/// <summary>Prevents every test from reaching the network and records requests before disposal.</summary>
internal sealed class ScriptedHttpHandler : HttpMessageHandler
{
    private readonly Queue<Func<CapturedRequest, HttpResponseMessage>> _responses = new();
    public List<CapturedRequest> Requests { get; } = [];

    public void EnqueueJson(HttpStatusCode status, string json) => _responses.Enqueue(_ =>
        new HttpResponseMessage(status)
        {
            Content = new StringContent(json, Encoding.UTF8, "application/json")
        });

    public void Enqueue(Func<CapturedRequest, HttpResponseMessage> response) => _responses.Enqueue(response);

    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var bytes = request.Content is null ? [] : await request.Content.ReadAsByteArrayAsync(cancellationToken);
        var capture = new CapturedRequest(
            request.Method,
            request.RequestUri,
            request.Headers.Authorization?.ToString(),
            request.Content?.Headers.ContentType?.ToString(),
            Encoding.UTF8.GetString(bytes));
        Requests.Add(capture);
        if (_responses.Count == 0)
            throw new TestFailure($"Unexpected HTTP request {request.Method} {request.RequestUri}.");
        return _responses.Dequeue()(capture);
    }
}
