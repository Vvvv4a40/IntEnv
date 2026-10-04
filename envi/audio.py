"""Bounded Windows microphone capture using only WinMM and the Python standard library.

The driver signals a Win32 event, not a Python callback. All native buffers stay alive
until waveInReset/unprepare/close have released the recording device.
"""

from __future__ import annotations

import ctypes
import struct
import sys
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any


_SAMPLE_RATE = 16_000
_BYTES_PER_SECOND = _SAMPLE_RATE * 2
_WAVE_MAPPER = 0xFFFFFFFF
_CALLBACK_EVENT = 0x00050000
_WHDR_DONE = 0x00000001
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x102
_WAIT_FAILED = 0xFFFFFFFF


class _WaveFormat(ctypes.Structure):
    _pack_ = 2
    _fields_ = [
        ("wFormatTag", ctypes.c_ushort),
        ("nChannels", ctypes.c_ushort),
        ("nSamplesPerSec", ctypes.c_uint32),
        ("nAvgBytesPerSec", ctypes.c_uint32),
        ("nBlockAlign", ctypes.c_ushort),
        ("wBitsPerSample", ctypes.c_ushort),
        ("cbSize", ctypes.c_ushort),
    ]


class _WaveHeader(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", ctypes.c_uint32),
        ("dwBytesRecorded", ctypes.c_uint32),
        ("dwUser", ctypes.c_void_p),
        ("dwFlags", ctypes.c_uint32),
        ("dwLoops", ctypes.c_uint32),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_void_p),
    ]


if sys.platform == "win32":
    _winmm = ctypes.WinDLL("winmm.dll", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)

    _winmm.waveInOpen.argtypes = [
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32,
        ctypes.POINTER(_WaveFormat), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    _winmm.waveInOpen.restype = ctypes.c_uint32
    for _name in ("waveInPrepareHeader", "waveInUnprepareHeader", "waveInAddBuffer"):
        _fn = getattr(_winmm, _name)
        _fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WaveHeader), ctypes.c_uint32]
        _fn.restype = ctypes.c_uint32
    for _name in ("waveInStart", "waveInReset", "waveInClose"):
        _fn = getattr(_winmm, _name)
        _fn.argtypes = [ctypes.c_void_p]
        _fn.restype = ctypes.c_uint32

    _kernel32.CreateEventW.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p,
    ]
    _kernel32.CreateEventW.restype = ctypes.c_void_p
    _kernel32.SetEvent.argtypes = [ctypes.c_void_p]
    _kernel32.SetEvent.restype = ctypes.c_int
    _kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    _kernel32.CloseHandle.restype = ctypes.c_int
else:
    _winmm = None
    _kernel32 = None


def _check(code: int, operation: str) -> None:
    if code:
        raise OSError(f"Не удалось {operation} (WinMM: {code}).")


def _wav_bytes(pcm: bytes) -> bytes:
    pcm = pcm[: len(pcm) & ~1]
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16,
        1, 1, _SAMPLE_RATE, _BYTES_PER_SECOND, 2, 16,
        b"data", len(pcm),
    )
    return header + pcm


@dataclass
class _Session:
    capacity: int
    maximum_seconds: int
    event_handle: int = 0
    device: ctypes.c_void_p = field(default_factory=ctypes.c_void_p)
    audio: Any = None
    header: _WaveHeader = field(default_factory=_WaveHeader)
    completion: Future[bytes] = field(default_factory=Future)
    stop_requested: threading.Event = field(default_factory=threading.Event)
    discard: threading.Event = field(default_factory=threading.Event)
    prepared: bool = False


class WaveRecorder:
    """One recording at a time. ``stop``/``cancel`` signal and return a Future.

    No method waits on the driver from the UI thread. ``completion`` resolves after
    native cleanup, with an in-memory PCM WAV or an exception. Cancel resolves to b"".
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: _Session | None = None

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._current is not None and not self._current.completion.done()

    @property
    def completion(self) -> Future[bytes]:
        with self._lock:
            if self._current is not None:
                return self._current.completion
        done: Future[bytes] = Future()
        done.set_result(b"")
        return done

    def start(self, maximum_seconds: int) -> Future[bytes]:
        if _winmm is None or _kernel32 is None:
            raise RuntimeError("Запись с микрофона поддерживается только в Windows.")
        if type(maximum_seconds) is not int or not 1 <= maximum_seconds <= 30:
            raise ValueError("Длительность записи должна быть от 1 до 30 секунд.")

        with self._lock:
            if self._current is not None:
                raise RuntimeError("Предыдущее аудиоустройство ещё не освобождено.")

            capacity = _BYTES_PER_SECOND * maximum_seconds
            session = _Session(capacity, maximum_seconds)
            self._current = session
            threading.Thread(target=self._wait_and_finish, args=(session,),
                             name="EnviWaveRecorder", daemon=True).start()
            return session.completion

    def stop(self) -> Future[bytes]:
        return self._signal(discard=False)

    def cancel(self) -> Future[bytes]:
        return self._signal(discard=True)

    def close(self) -> Future[bytes]:
        """Request cancellation without blocking the UI; await the returned Future."""
        return self.cancel()

    def _signal(self, discard: bool) -> Future[bytes]:
        with self._lock:
            session = self._current
            if session is None:
                done: Future[bytes] = Future()
                done.set_result(b"")
                return done
            if discard:
                session.discard.set()
            session.stop_requested.set()
            if _kernel32 is not None and session.event_handle:
                _kernel32.SetEvent(ctypes.c_void_p(session.event_handle))
            return session.completion

    def _wait_and_finish(self, session: _Session) -> None:
        assert _winmm is not None and _kernel32 is not None
        data = b""
        error: Exception | None = None
        try:
            # Device opening and every potentially blocking driver call run in
            # this worker, never in tkinter's main thread.
            if session.stop_requested.is_set():
                return
            handle = _kernel32.CreateEventW(None, False, False, None)
            if not handle:
                raise OSError(ctypes.get_last_error(), "Не удалось создать событие микрофона.")
            with self._lock:
                session.event_handle = handle
            session.audio = ctypes.create_string_buffer(session.capacity)
            session.header.lpData = ctypes.addressof(session.audio)
            session.header.dwBufferLength = session.capacity
            wave_format = _WaveFormat(1, 1, _SAMPLE_RATE, _BYTES_PER_SECOND, 2, 16, 0)
            _check(_winmm.waveInOpen(
                ctypes.byref(session.device), _WAVE_MAPPER,
                ctypes.byref(wave_format), ctypes.c_void_p(handle),
                None, _CALLBACK_EVENT,
            ), "открыть микрофон")
            _check(_winmm.waveInPrepareHeader(
                session.device, ctypes.byref(session.header), ctypes.sizeof(session.header),
            ), "подготовить аудиобуфер")
            session.prepared = True
            _check(_winmm.waveInAddBuffer(
                session.device, ctypes.byref(session.header), ctypes.sizeof(session.header),
            ), "передать аудиобуфер устройству")
            if not session.stop_requested.is_set():
                _check(_winmm.waveInStart(session.device), "начать запись")
            deadline = time.monotonic() + session.maximum_seconds + 1.0
            while not session.stop_requested.is_set() and not (session.header.dwFlags & _WHDR_DONE):
                remaining_ms = max(1, min(250, int((deadline - time.monotonic()) * 1000)))
                if time.monotonic() >= deadline:
                    break
                result = _kernel32.WaitForSingleObject(
                    ctypes.c_void_p(session.event_handle), remaining_ms,
                )
                if result not in (_WAIT_OBJECT_0, _WAIT_TIMEOUT):
                    raise OSError(ctypes.get_last_error(), "Ошибка ожидания микрофона.")

            # waveInReset returns any partly filled buffer before we inspect it.
            _check(_winmm.waveInReset(session.device), "остановить запись")
            if not session.discard.is_set():
                count = min(session.header.dwBytesRecorded, session.capacity)
                data = _wav_bytes(ctypes.string_at(session.header.lpData, count))
        except Exception as exc:
            error = exc
        finally:
            cleanup_error, released = self._release_native(session, reset_first=error is not None)
            if error is None:
                error = cleanup_error
            with self._lock:
                if released:
                    if session.event_handle and not _kernel32.CloseHandle(
                            ctypes.c_void_p(session.event_handle)) and error is None:
                        error = OSError(ctypes.get_last_error(), "Не удалось закрыть событие микрофона.")
                    session.event_handle = 0
                    self._current = None
                # If a broken driver still owns the buffer, retain the whole
                # session. Future start attempts fail safely instead of freeing
                # native memory that the driver might still be using.
            # Future callbacks are intentionally outside our lock: callers may
            # inspect the recorder from a completion callback without deadlock.
            if error is None:
                session.completion.set_result(b"" if session.discard.is_set() else data)
            else:
                session.completion.set_exception(error)

    @staticmethod
    def _release_native(session: _Session, *, reset_first: bool) -> tuple[Exception | None, bool]:
        assert _winmm is not None
        error: Exception | None = None
        if session.device.value and reset_first:
            code = _winmm.waveInReset(session.device)
            if code:
                error = OSError(f"Не удалось остановить устройство (WinMM: {code}).")
        if session.device.value and session.prepared:
            code = _winmm.waveInUnprepareHeader(
                session.device, ctypes.byref(session.header), ctypes.sizeof(session.header),
            )
            if code:
                error = error or OSError(f"Не удалось освободить аудиобуфер (WinMM: {code}).")
            else:
                session.prepared = False
        if session.device.value:
            code = _winmm.waveInClose(session.device)
            if code:
                error = error or OSError(f"Не удалось закрыть микрофон (WinMM: {code}).")
            else:
                session.device = ctypes.c_void_p()
        return error, not session.device.value and not session.prepared
