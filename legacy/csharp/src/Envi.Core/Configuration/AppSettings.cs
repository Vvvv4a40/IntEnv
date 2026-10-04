using System.Text.Json;
using System.Text.Json.Serialization;

namespace Envi.Core.Configuration;

public sealed class AppSettings
{
    public string ChatModel { get; set; } = "openai/gpt-oss-20b";
    public string SttModel { get; set; } = "whisper-large-v3-turbo";
    public string Language { get; set; } = "ru";
    public int TimeoutSeconds { get; set; } = 30;
    public int MaxToolRounds { get; set; } = 3;
    public int MaxOutputTokens { get; set; } = 600;
    public int HistoryMessages { get; set; } = 12;
    public int MaxSessionApiRequests { get; set; } = 100;
    public int RecordingMaxSeconds { get; set; } = 30;
    public Dictionary<string, string> Apps { get; set; } = new(StringComparer.OrdinalIgnoreCase);
    public Dictionary<string, string> Folders { get; set; } = new(StringComparer.OrdinalIgnoreCase);
    [JsonIgnore] public string BaseDirectory { get; set; } = AppContext.BaseDirectory;

    public static AppSettings Load(string path)
    {
        try
        {
            var fullPath = Path.GetFullPath(path);
            var settings = JsonSerializer.Deserialize<AppSettings>(File.ReadAllText(fullPath),
                new JsonSerializerOptions { PropertyNameCaseInsensitive = true, UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow })
                ?? throw new AssistantException("Файл настроек пуст.");
            settings.BaseDirectory = Path.GetDirectoryName(fullPath)!;
            settings.Validate();
            settings.Apps = new(settings.Apps, StringComparer.OrdinalIgnoreCase);
            settings.Folders = new(settings.Folders, StringComparer.OrdinalIgnoreCase);
            return settings;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException or ArgumentException)
        {
            throw new AssistantException("Не удалось прочитать настройки. Проверь путь и JSON в envi.settings.json.", ex);
        }
    }

    public void Validate()
    {
        if (string.IsNullOrWhiteSpace(ChatModel) || string.IsNullOrWhiteSpace(SttModel) ||
            string.IsNullOrWhiteSpace(Language) || Language.Length > 8 ||
            TimeoutSeconds is < 5 or > 120 || MaxToolRounds is < 1 or > 5 ||
            MaxOutputTokens is < 128 or > 4096 || HistoryMessages is < 0 or > 40 ||
            MaxSessionApiRequests is < 1 or > 1000 || RecordingMaxSeconds is < 1 or > 30 ||
            Apps is null || Folders is null)
            throw new AssistantException("Некорректные настройки: проверь модели, списки разрешений и числовые лимиты.");

        foreach (var entry in Apps.Concat(Folders))
        {
            if (entry.Key.Length is < 1 or > 40 || !entry.Key.All(c => char.IsAsciiLetterOrDigit(c) || c is '_' or '-') ||
                string.IsNullOrWhiteSpace(entry.Value))
                throw new AssistantException("В списках разрешений нужны короткие имена и непустые пути.");
        }
    }
}
