using Envi.Core;
using Envi.Core.Configuration;
using Envi.Core.Routing;
using Envi.Core.Tools;

namespace Envi.Tests;

internal static class RoutingAndToolsTests
{
    public static void Register(TestRunner runner)
    {
        runner.Add("router: only complete, unambiguous phrases route locally", RouterRejectsAmbiguity);
        runner.Add("tools: reject arbitrary commands, paths, extra keys and malformed arguments", ValidationRejectsUnsafeArguments);
        runner.Add("tools: only configured aliases are in schemas", SchemasUseAllowlists);
        runner.Add("tools: relative folders cannot escape configured base directory", FolderTraversalIsRejected);
        runner.Add("tools: get_time is safe and other actions need confirmation", ConfirmationPolicy);
    }

    private static void RouterRejectsAmbiguity()
    {
        var router = new RuleRouter();
        Check.Equal("get_time", router.TryRoute("Который час?")?.Name);
        Check.Equal("open_app", router.TryRoute("Открой блокнот")?.Name);
        Check.Equal("open_folder", router.TryRoute("Открой папку проекта")?.Name);

        string[] nonCommands =
        [
            "Который час и открой блокнот",
            "Не открывай блокнот",
            "Если будет время, открой калькулятор",
            "Открой блокнот; удали файлы",
            "Сколько времени до встречи?",
            "Открой папку проекта и запусти shell",
            "открой калькулятор\nзатем сделай перевод",
            "Запусти PowerShell"
        ];
        foreach (string text in nonCommands)
            Check.True(router.TryRoute(text) is null, $"Unsafe or ambiguous phrase routed locally: {text}");
    }

    private static void ValidationRejectsUnsafeArguments()
    {
        var registry = new ToolRegistry(Settings());
        ToolCall[] invalid =
        [
            new("run_powershell", "{\"command\":\"Remove-Item C:\\\\Users\"}"),
            new("open_app", "{\"app\":\"C:\\\\Windows\\\\System32\\\\cmd.exe\"}"),
            new("open_app", "{\"app\":\"notepad\",\"arguments\":\"/c del *\"}"),
            new("open_app", "{\"app\":42}"),
            new("open_app", "{\"app\":\"notepad\",\"app\":\"calc\"}"),
            new("open_app", "{\"app\":\"notepad\"}"),
            new("open_folder", "{\"folder\":\"C:\\\\Users\"}"),
            new("get_time", "{\"command\":\"shutdown\"}"),
            new("get_time", "[]"),
            new("get_time", "{"),
            new("get_time", "")
        ];
        foreach (ToolCall call in invalid)
            Check.True(registry.Validate(call) is not null, $"Unsafe call accepted: {call.Name} {call.ArgumentsJson}");

        Check.True(registry.Validate(new ToolCall("get_time", "{}")) is null);
    }

    private static void SchemasUseAllowlists()
    {
        var settings = Settings();
        settings.Apps["safe_app"] = @"C:\EnviTest\safe_app.exe";
        settings.Folders["project"] = ".";
        var registry = new ToolRegistry(settings);
        string json = System.Text.Json.JsonSerializer.Serialize(registry.Schemas);
        Check.Contains("get_time", json);
        Check.Contains("open_app", json);
        Check.Contains("safe_app", json);
        Check.Contains("open_folder", json);
        Check.Contains("project", json);
        Check.False(json.Contains("run_powershell", StringComparison.OrdinalIgnoreCase));
        Check.False(json.Contains("C:\\EnviTest", StringComparison.OrdinalIgnoreCase), "Schemas should expose aliases, not paths.");
        Check.Contains("additionalProperties", json);
    }

    private static void FolderTraversalIsRejected()
    {
        var settings = Settings();
        settings.BaseDirectory = Path.GetFullPath(Directory.GetCurrentDirectory());
        settings.Folders["project"] = ".";
        settings.Folders["escape"] = "..";
        var registry = new ToolRegistry(settings);
        Check.True(registry.Validate(new ToolCall("open_folder", "{\"folder\":\"project\"}")) is null);
        Check.True(registry.Validate(new ToolCall("open_folder", "{\"folder\":\"escape\"}")) is not null);
        Check.True(registry.Validate(new ToolCall("open_folder", "{\"folder\":\"arbitrary\"}")) is not null);
    }

    private static void ConfirmationPolicy()
    {
        var registry = new ToolRegistry(Settings());
        Check.False(registry.RequiresConfirmation(new ToolCall("get_time", "{}")));
        Check.True(registry.RequiresConfirmation(new ToolCall("open_app", "{\"app\":\"anything\"}")));
        Check.True(registry.RequiresConfirmation(new ToolCall("open_folder", "{\"folder\":\"anything\"}")));
    }

    private static AppSettings Settings() => new() { BaseDirectory = Directory.GetCurrentDirectory() };
}
