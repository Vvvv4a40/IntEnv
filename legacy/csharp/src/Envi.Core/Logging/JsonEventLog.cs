using System.Text;
using System.Text.Json;

namespace Envi.Core.Logging;

/// <summary>Metadata-only log. Callers must never pass keys, audio, prompts or full provider responses.</summary>
public sealed class JsonEventLog(string path) : IEventSink
{
    private readonly object _gate = new();
    public string? LastError { get; private set; }

    public void Record(string eventType, object? data = null)
    {
        lock (_gate)
        {
            try
            {
                var folder = Path.GetDirectoryName(Path.GetFullPath(path))!;
                Directory.CreateDirectory(folder);
                var line = JsonSerializer.Serialize(new { timestamp = DateTimeOffset.UtcNow, eventType, data });
                File.AppendAllText(path, line + Environment.NewLine, Encoding.UTF8);
                LastError = null;
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
            {
                LastError = "Не удалось записать журнал событий.";
            }
        }
    }
}
