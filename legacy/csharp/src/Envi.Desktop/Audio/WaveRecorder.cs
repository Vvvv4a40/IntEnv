using System.IO;
using System.Runtime.InteropServices;
using System.Text;

namespace Envi.Desktop.Audio;

/// <summary>
/// Records a single, bounded 16 kHz/16-bit/mono PCM buffer and returns a WAV file in memory.
/// WinMM is used without a managed native callback: CALLBACK_EVENT only wakes a worker thread.
/// </summary>
public sealed class WaveRecorder : IDisposable
{
    private const uint WaveMapper = uint.MaxValue;
    private const uint CallbackEvent = 0x00050000;
    private const uint WaveHeaderDone = 0x00000001;
    private const int SampleRate = 16_000;
    private const int BytesPerSecond = SampleRate * sizeof(short);

    private readonly object _gate = new();
    private AutoResetEvent? _bufferReady;
    private TaskCompletionSource<byte[]>? _completion;
    private IntPtr _device;
    private IntPtr _audioBuffer;
    private IntPtr _headerBuffer;
    private int _capacity;
    private bool _prepared;
    private bool _active;
    private bool _stopRequested;
    private bool _discard;
    private bool _disposed;

    public bool IsRecording
    {
        get { lock (_gate) return _active; }
    }

    public Task<byte[]> Completion
    {
        get { lock (_gate) return _completion?.Task ?? Task.FromResult(Array.Empty<byte>()); }
    }

    public void Start(int maximumSeconds)
    {
        lock (_gate)
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            if (_active)
                throw new InvalidOperationException("Запись уже идёт.");
            if (_device != IntPtr.Zero || _headerBuffer != IntPtr.Zero || _bufferReady is not null)
                throw new InvalidOperationException("Аудиоустройство не было полностью освобождено после предыдущей записи.");

            _capacity = checked(BytesPerSecond * Math.Clamp(maximumSeconds, 1, 30));
            _bufferReady = new AutoResetEvent(false);
            _completion = new TaskCompletionSource<byte[]>(TaskCreationOptions.RunContinuationsAsynchronously);
            _stopRequested = false;
            _discard = false;

            try
            {
                var format = new WaveFormat
                {
                    FormatTag = 1, // WAVE_FORMAT_PCM
                    Channels = 1,
                    SamplesPerSecond = SampleRate,
                    AverageBytesPerSecond = BytesPerSecond,
                    BlockAlignment = sizeof(short),
                    BitsPerSample = 16,
                    ExtraSize = 0
                };

                Check(waveInOpen(out _device, WaveMapper, ref format,
                    _bufferReady.SafeWaitHandle.DangerousGetHandle(), IntPtr.Zero, CallbackEvent),
                    "открыть микрофон");

                _audioBuffer = Marshal.AllocHGlobal(_capacity);
                _headerBuffer = Marshal.AllocHGlobal(Marshal.SizeOf<WaveHeader>());
                Marshal.StructureToPtr(new WaveHeader
                {
                    Data = _audioBuffer,
                    BufferLength = (uint)_capacity
                }, _headerBuffer, false);

                Check(waveInPrepareHeader(_device, _headerBuffer, (uint)Marshal.SizeOf<WaveHeader>()),
                    "подготовить аудиобуфер");
                _prepared = true;
                Check(waveInAddBuffer(_device, _headerBuffer, (uint)Marshal.SizeOf<WaveHeader>()),
                    "передать аудиобуфер устройству");
                Check(waveInStart(_device), "начать запись");

                _active = true;
                _ = Task.Run(WaitForBuffer);
            }
            catch
            {
                ReleaseNative(resetFirst: true);
                _completion = null;
                throw;
            }
        }
    }

    public Task<byte[]> StopAsync()
    {
        lock (_gate)
        {
            Task<byte[]> task = _completion?.Task ?? Task.FromResult(Array.Empty<byte>());
            if (_active)
            {
                _stopRequested = true;
                _bufferReady?.Set();
            }
            return task;
        }
    }

    public Task<byte[]> CancelAsync()
    {
        lock (_gate)
        {
            if (_active)
            {
                _discard = true;
                _stopRequested = true;
                _bufferReady?.Set();
            }
            return _completion?.Task ?? Task.FromResult(Array.Empty<byte>());
        }
    }

    private void WaitForBuffer()
    {
        AutoResetEvent? signal = _bufferReady;
        if (signal is null)
            return;

        while (true)
        {
            signal.WaitOne();
            lock (_gate)
            {
                if (!_active)
                    return;

                var header = Marshal.PtrToStructure<WaveHeader>(_headerBuffer);
                if (!_stopRequested && (header.Flags & WaveHeaderDone) == 0)
                    continue;

                Exception? error = null;
                byte[] wav = Array.Empty<byte>();
                try
                {
                    // Reset returns even a partly filled buffer before it is inspected or freed.
                    Check(waveInReset(_device), "остановить запись");
                    header = Marshal.PtrToStructure<WaveHeader>(_headerBuffer);
                    if (!_discard)
                    {
                        int length = (int)Math.Min(header.BytesRecorded, (uint)_capacity);
                        byte[] pcm = new byte[length];
                        if (length != 0)
                            Marshal.Copy(_audioBuffer, pcm, 0, length);
                        wav = CreateWav(pcm);
                    }
                }
                catch (Exception exception)
                {
                    error = exception;
                }

                Exception? cleanupError = ReleaseNative(resetFirst: error is not null);
                error ??= cleanupError;
                _active = false;
                if (error is null)
                    _completion?.TrySetResult(wav);
                else
                    _completion?.TrySetException(error);
                return;
            }
        }
    }

    private Exception? ReleaseNative(bool resetFirst)
    {
        Exception? error = null;
        if (_device != IntPtr.Zero && resetFirst)
        {
            uint result = waveInReset(_device);
            if (result != 0)
                error = MmError("остановить устройство", result);
        }

        if (_device != IntPtr.Zero && _prepared)
        {
            uint result = waveInUnprepareHeader(_device, _headerBuffer, (uint)Marshal.SizeOf<WaveHeader>());
            if (result == 0)
                _prepared = false;
            else
                error ??= MmError("освободить аудиобуфер", result);
        }

        if (_device != IntPtr.Zero)
        {
            uint result = waveInClose(_device);
            if (result == 0)
                _device = IntPtr.Zero;
            else
                error ??= MmError("закрыть микрофон", result);
        }

        // Never release memory or the event while the driver may still reference them.
        if (_device == IntPtr.Zero && !_prepared)
        {
            if (_headerBuffer != IntPtr.Zero)
                Marshal.FreeHGlobal(_headerBuffer);
            if (_audioBuffer != IntPtr.Zero)
                Marshal.FreeHGlobal(_audioBuffer);
            _headerBuffer = IntPtr.Zero;
            _audioBuffer = IntPtr.Zero;
            _bufferReady?.Dispose();
            _bufferReady = null;
        }
        return error;
    }

    private static byte[] CreateWav(byte[] pcm)
    {
        using var stream = new MemoryStream(44 + pcm.Length);
        using var writer = new BinaryWriter(stream, Encoding.ASCII, leaveOpen: true);
        writer.Write(Encoding.ASCII.GetBytes("RIFF"));
        writer.Write(36 + pcm.Length);
        writer.Write(Encoding.ASCII.GetBytes("WAVE"));
        writer.Write(Encoding.ASCII.GetBytes("fmt "));
        writer.Write(16);
        writer.Write((short)1);
        writer.Write((short)1);
        writer.Write(SampleRate);
        writer.Write(BytesPerSecond);
        writer.Write((short)sizeof(short));
        writer.Write((short)16);
        writer.Write(Encoding.ASCII.GetBytes("data"));
        writer.Write(pcm.Length);
        writer.Write(pcm);
        writer.Flush();
        return stream.ToArray();
    }

    private static void Check(uint result, string action)
    {
        if (result != 0)
            throw MmError(action, result);
    }

    private static Exception MmError(string action, uint code) =>
        new InvalidOperationException($"Не удалось {action} (WinMM: {code}).");

    public void Dispose()
    {
        if (_disposed)
            return;
        CancelAsync().GetAwaiter().GetResult();
        _disposed = true;
    }

    [StructLayout(LayoutKind.Sequential, Pack = 2)]
    private struct WaveFormat
    {
        public ushort FormatTag;
        public ushort Channels;
        public uint SamplesPerSecond;
        public uint AverageBytesPerSecond;
        public ushort BlockAlignment;
        public ushort BitsPerSample;
        public ushort ExtraSize;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct WaveHeader
    {
        public IntPtr Data;
        public uint BufferLength;
        public uint BytesRecorded;
        public IntPtr User;
        public uint Flags;
        public uint Loops;
        public IntPtr Next;
        public IntPtr Reserved;
    }

    [DllImport("winmm.dll", ExactSpelling = true)]
    private static extern uint waveInOpen(out IntPtr device, uint deviceId, ref WaveFormat format,
        IntPtr callback, IntPtr instance, uint flags);

    [DllImport("winmm.dll", ExactSpelling = true)]
    private static extern uint waveInPrepareHeader(IntPtr device, IntPtr header, uint size);

    [DllImport("winmm.dll", ExactSpelling = true)]
    private static extern uint waveInUnprepareHeader(IntPtr device, IntPtr header, uint size);

    [DllImport("winmm.dll", ExactSpelling = true)]
    private static extern uint waveInAddBuffer(IntPtr device, IntPtr header, uint size);

    [DllImport("winmm.dll", ExactSpelling = true)]
    private static extern uint waveInStart(IntPtr device);

    [DllImport("winmm.dll", ExactSpelling = true)]
    private static extern uint waveInReset(IntPtr device);

    [DllImport("winmm.dll", ExactSpelling = true)]
    private static extern uint waveInClose(IntPtr device);
}
