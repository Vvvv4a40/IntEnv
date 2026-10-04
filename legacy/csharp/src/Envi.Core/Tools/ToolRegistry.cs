using System.Diagnostics;
using System.Globalization;
using System.Text.Json;
using Envi.Core.Configuration;

namespace Envi.Core.Tools;

/// <summary>
/// Exposes only explicitly configured, narrowly scoped actions to the model.
/// The same validation is performed again immediately before execution.
/// </summary>
public sealed class ToolRegistry : IToolRegistry
{
    private const string GetTime = "get_time";
    private const string OpenApp = "open_app";
    private const string OpenFolder = "open_folder";

    private static readonly HashSet<string> BlockedExecutables = new(StringComparer.OrdinalIgnoreCase)
    {
        "cmd.exe", "powershell.exe", "pwsh.exe", "wsl.exe", "bash.exe", "sh.exe",
        "wscript.exe", "cscript.exe", "mshta.exe"
    };

    private readonly string _baseDirectory;
    private readonly Dictionary<string, string> _apps;
    private readonly Dictionary<string, string> _folders;

    public ToolRegistry(AppSettings settings)
    {
        ArgumentNullException.ThrowIfNull(settings);
        _baseDirectory = settings.BaseDirectory;
        _apps = CopyAliases(settings.Apps);
        _folders = CopyAliases(settings.Folders);
        Schemas = BuildSchemas();
    }

    /// <summary>OpenAI-compatible function schemas for the configured allowlists.</summary>
    public IReadOnlyList<object> Schemas { get; }

    /// <summary>Returns a Russian validation error, or null when the call is safe to consider.</summary>
    public string? Validate(ToolCall call)
    {
        if (call is null)
            return "Вызов инструмента отсутствует.";

        string? expectedProperty = call.Name switch
        {
            GetTime => null,
            OpenApp => "app",
            OpenFolder => "folder",
            _ => ""
        };
        if (expectedProperty == "")
            return "Неизвестный инструмент.";

        if (string.IsNullOrWhiteSpace(call.ArgumentsJson))
            return "Аргументы инструмента должны быть объектом JSON.";

        try
        {
            using JsonDocument document = JsonDocument.Parse(call.ArgumentsJson, new JsonDocumentOptions
            {
                AllowTrailingCommas = false,
                CommentHandling = JsonCommentHandling.Disallow,
                MaxDepth = 8
            });

            if (document.RootElement.ValueKind != JsonValueKind.Object)
                return "Аргументы инструмента должны быть объектом JSON.";

            JsonProperty? argument = null;
            foreach (JsonProperty property in document.RootElement.EnumerateObject())
            {
                // Reject unexpected and duplicate properties, including path and shell commands.
                if (expectedProperty is null || property.Name != expectedProperty || argument is not null)
                    return "У инструмента есть лишние или повторяющиеся аргументы.";
                argument = property;
            }

            if (expectedProperty is null)
                return null;

            if (argument is null || argument.Value.Value.ValueKind != JsonValueKind.String)
                return $"Аргумент {expectedProperty} должен быть строкой из разрешённого списка.";

            string? alias = argument.Value.Value.GetString();
            if (string.IsNullOrWhiteSpace(alias))
                return $"Аргумент {expectedProperty} должен быть строкой из разрешённого списка.";

            if (call.Name == OpenApp)
            {
                if (!_apps.TryGetValue(alias, out string? configuredPath))
                    return "Приложение отсутствует в разрешённом списке.";
                return ResolveAppPath(configuredPath, out _);
            }

            if (!_folders.TryGetValue(alias, out string? configuredFolder))
                return "Папка отсутствует в разрешённом списке.";
            return ResolveFolderPath(configuredFolder, out _);
        }
        catch (JsonException)
        {
            return "Не удалось разобрать JSON аргументов инструмента.";
        }
    }

    /// <summary>Opening an app or folder always needs explicit user confirmation.</summary>
    public bool RequiresConfirmation(ToolCall call) =>
        call is null || !string.Equals(call.Name, GetTime, StringComparison.Ordinal);

    /// <summary>Describes the exact configured target shown to the user before confirmation.</summary>
    public string Describe(ToolCall call)
    {
        string? error = Validate(call);
        if (error is not null)
            throw new ArgumentException(error, nameof(call));

        return call.Name switch
        {
            GetTime => "Показать текущее местное время.",
            OpenApp => DescribeApp(ReadAlias(call.ArgumentsJson, "app")),
            OpenFolder => DescribeFolder(ReadAlias(call.ArgumentsJson, "folder")),
            _ => throw new InvalidOperationException("Неизвестный инструмент.")
        };
    }

    /// <summary>Executes only a validated, configured action on the Windows host.</summary>
    public Task<string> ExecuteAsync(ToolCall call, CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        string? error = Validate(call);
        if (error is not null)
            throw new ArgumentException(error, nameof(call));

        if (call.Name == GetTime)
        {
            DateTimeOffset now = DateTimeOffset.Now;
            return Task.FromResult("Сейчас " + now.ToString("dd.MM.yyyy HH:mm:ss zzz", CultureInfo.GetCultureInfo("ru-RU")) + ".");
        }

        if (!OperatingSystem.IsWindows())
            throw new PlatformNotSupportedException("Открытие приложений и папок доступно только в Windows.");

        cancellationToken.ThrowIfCancellationRequested();
        if (call.Name == OpenApp)
        {
            string alias = ReadAlias(call.ArgumentsJson, "app");
            string? pathError = ResolveAppPath(_apps[alias], out string executablePath);
            if (pathError is not null)
                throw new InvalidOperationException(pathError);

            using Process? process = Process.Start(new ProcessStartInfo
            {
                FileName = executablePath,
                UseShellExecute = true
            });
            return Task.FromResult($"Запуск приложения «{alias}» запрошен.");
        }

        string folderAlias = ReadAlias(call.ArgumentsJson, "folder");
        string? folderError = ResolveFolderPath(_folders[folderAlias], out string folderPath);
        if (folderError is not null)
            throw new InvalidOperationException(folderError);

        string explorerPath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "explorer.exe");
        if (!File.Exists(explorerPath))
            throw new FileNotFoundException("Не найден Проводник Windows.", explorerPath);

        var startInfo = new ProcessStartInfo
        {
            FileName = explorerPath,
            UseShellExecute = false
        };
        startInfo.ArgumentList.Add(folderPath);
        using Process? explorer = Process.Start(startInfo);
        return Task.FromResult($"Открытие папки «{folderAlias}» запрошено.");
    }

    private string DescribeApp(string alias)
    {
        string? error = ResolveAppPath(_apps[alias], out string path);
        if (error is not null)
            throw new InvalidOperationException(error);
        return $"Открыть приложение «{alias}»: {path}";
    }

    private string DescribeFolder(string alias)
    {
        string? error = ResolveFolderPath(_folders[alias], out string path);
        if (error is not null)
            throw new InvalidOperationException(error);
        return $"Открыть папку «{alias}»: {path}";
    }

    private static Dictionary<string, string> CopyAliases(Dictionary<string, string> aliases)
    {
        var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach ((string alias, string configuredPath) in aliases)
        {
            if (string.IsNullOrWhiteSpace(alias) || !result.TryAdd(alias, configuredPath))
                throw new ArgumentException("В настройках есть пустой или повторяющийся псевдоним.", nameof(aliases));
        }
        return result;
    }

    private IReadOnlyList<object> BuildSchemas()
    {
        var result = new List<object> { Schema(GetTime, "Получить текущее местное время.", null, null) };
        if (_apps.Count > 0)
            result.Add(Schema(OpenApp, "Открыть разрешённое приложение Windows. Требуется подтверждение пользователя.", "app", _apps.Keys));
        if (_folders.Count > 0)
            result.Add(Schema(OpenFolder, "Открыть разрешённую папку в Проводнике Windows. Требуется подтверждение пользователя.", "folder", _folders.Keys));
        return result.AsReadOnly();
    }

    private static object Schema(string name, string description, string? argumentName, IEnumerable<string>? aliases)
    {
        var properties = new Dictionary<string, object>();
        if (argumentName is not null)
        {
            properties[argumentName] = new Dictionary<string, object>
            {
                ["type"] = "string",
                ["enum"] = aliases!.OrderBy(alias => alias, StringComparer.OrdinalIgnoreCase).ToArray()
            };
        }

        return new Dictionary<string, object>
        {
            ["type"] = "function",
            ["function"] = new Dictionary<string, object>
            {
                ["name"] = name,
                ["description"] = description,
                ["parameters"] = new Dictionary<string, object>
                {
                    ["type"] = "object",
                    ["properties"] = properties,
                    ["required"] = argumentName is null ? Array.Empty<string>() : new[] { argumentName },
                    ["additionalProperties"] = false
                }
            }
        };
    }

    private static string ReadAlias(string argumentsJson, string propertyName)
    {
        using JsonDocument document = JsonDocument.Parse(argumentsJson);
        return document.RootElement.GetProperty(propertyName).GetString()!;
    }

    private static string? ResolveAppPath(string configuredPath, out string fullPath)
    {
        fullPath = "";
        if (string.IsNullOrWhiteSpace(configuredPath))
            return "Для приложения не задан путь в настройках.";

        try
        {
            string expanded = Environment.ExpandEnvironmentVariables(configuredPath);
            if (!Path.IsPathFullyQualified(expanded) ||
                !string.Equals(Path.GetExtension(expanded), ".exe", StringComparison.OrdinalIgnoreCase))
                return "В настройках приложения требуется полный путь к файлу .exe.";
            fullPath = Path.GetFullPath(expanded);
        }
        catch (Exception ex) when (ex is ArgumentException or NotSupportedException or PathTooLongException)
        {
            return "Путь к приложению в настройках некорректен.";
        }

        if (BlockedExecutables.Contains(Path.GetFileName(fullPath)))
            return "Запуск командной оболочки не разрешён.";
        if (!File.Exists(fullPath))
            return "Файл приложения из настроек не найден.";
        return null;
    }

    private string? ResolveFolderPath(string configuredFolder, out string fullPath)
    {
        fullPath = "";
        if (string.IsNullOrWhiteSpace(configuredFolder))
            return "Для папки не задан путь в настройках.";

        try
        {
            string expanded = Environment.ExpandEnvironmentVariables(configuredFolder);
            string baseDirectory = Path.GetFullPath(_baseDirectory);
            if (Path.IsPathRooted(expanded) && !Path.IsPathFullyQualified(expanded))
                return "Путь к папке в настройках должен быть полным или относительным от каталога проекта.";

            fullPath = Path.IsPathFullyQualified(expanded)
                ? Path.GetFullPath(expanded)
                : Path.GetFullPath(expanded, baseDirectory);

            if (!Path.IsPathFullyQualified(expanded) &&
                !IsWithinDirectory(fullPath, baseDirectory))
                return "Относительный путь к папке выходит за пределы каталога проекта.";
        }
        catch (Exception ex) when (ex is ArgumentException or NotSupportedException or PathTooLongException)
        {
            return "Путь к папке в настройках некорректен.";
        }

        if (!Directory.Exists(fullPath))
            return "Папка из настроек не найдена.";
        return null;
    }

    private static bool IsWithinDirectory(string path, string directory) =>
        path.Equals(directory, StringComparison.OrdinalIgnoreCase) ||
        path.StartsWith(Path.TrimEndingDirectorySeparator(directory) + Path.DirectorySeparatorChar,
            StringComparison.OrdinalIgnoreCase);
}
