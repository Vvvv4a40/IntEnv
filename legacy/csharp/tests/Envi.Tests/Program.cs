namespace Envi.Tests;

internal static class Program
{
    private static Task<int> Main(string[] args)
    {
        var runner = new TestRunner();
        RoutingAndToolsTests.Register(runner);
        AssistantTests.Register(runner);
        GroqClientTests.Register(runner);
        SafetyAndConfigTests.Register(runner);
        PartialActionTests.Register(runner);
        return runner.RunAsync(args);
    }
}
