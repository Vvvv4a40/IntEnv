using System.Text.RegularExpressions;

namespace Envi.Core.Routing;

/// <summary>
/// Routes only a few complete, unambiguous phrases without a cloud request.
/// Everything else is left for the model or for clarification.
/// </summary>
public sealed class RuleRouter
{
    private static readonly Regex Whitespace = new(@"\s+", RegexOptions.Compiled | RegexOptions.CultureInvariant);

    private static readonly Regex TimePhrase = Phrase(
        @"(?:сколько(?: сейчас)? времени|который час|скажи время|покажи время|what time is it|tell me the time)");

    private static readonly Regex ProjectFolderPhrase = Phrase(
        @"(?:(?:открой|покажи) папку проекта|(?:open|show) (?:the )?project folder)");

    private static readonly Regex NotepadPhrase = Phrase(
        @"(?:(?:открой|запусти) (?:блокнот|notepad)|(?:open|launch) notepad)");

    private static readonly Regex CalculatorPhrase = Phrase(
        @"(?:(?:открой|запусти) (?:калькулятор|calculator|calc)|(?:open|launch) (?:calculator|calc))");

    private static readonly Regex VsCodePhrase = Phrase(
        @"(?:(?:открой|запусти) (?:vs code|vscode|visual studio code)|(?:open|launch) (?:vs code|vscode|visual studio code))");

    public ToolCall? TryRoute(string text)
    {
        if (string.IsNullOrWhiteSpace(text) || text.Length > 256)
            return null;

        string normalized = Whitespace.Replace(text.Trim().ToLowerInvariant().Replace('ё', 'е'), " ");

        if (TimePhrase.IsMatch(normalized))
            return new ToolCall("get_time", "{}");
        if (ProjectFolderPhrase.IsMatch(normalized))
            return new ToolCall("open_folder", "{\"folder\":\"project\"}");
        if (NotepadPhrase.IsMatch(normalized))
            return new ToolCall("open_app", "{\"app\":\"notepad\"}");
        if (CalculatorPhrase.IsMatch(normalized))
            return new ToolCall("open_app", "{\"app\":\"calculator\"}");
        if (VsCodePhrase.IsMatch(normalized))
            return new ToolCall("open_app", "{\"app\":\"vscode\"}");

        return null;
    }

    private static Regex Phrase(string body) => new(
        @"\A" + body + @"[?.!]?\z",
        RegexOptions.Compiled | RegexOptions.CultureInvariant,
        TimeSpan.FromMilliseconds(100));
}
