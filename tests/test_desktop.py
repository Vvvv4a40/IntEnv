"""Desktop/audio tests using hidden Tk widgets and fake WinMM, never real devices or APIs."""

import ctypes
import gc
import struct
import threading
import time
import unittest
import weakref
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import patch

from envi import audio
from envi.configuration import AppSettings
from envi.models import ToolCall

try:
    import tkinter as tk
    from envi.ui import EnviWindow
except ImportError:
    tk = None
    EnviWindow = None


class FakeKernel:
    def __init__(self):
        self.closed = []

    def CreateEventW(self, *_args):
        return 7

    def SetEvent(self, *_args):
        return 1

    def WaitForSingleObject(self, *_args):
        return audio._WAIT_TIMEOUT

    def CloseHandle(self, handle):
        self.closed.append(handle.value)
        return 1


class FakeWinMM:
    def __init__(self, open_gate=None, cleanup_failure=False):
        self.open_gate = open_gate
        self.entered = threading.Event()
        self.cleanup_failure = cleanup_failure
        self.operations = []
        self.header = None

    def waveInOpen(self, destination, *_args):
        self.operations.append("open")
        self.entered.set()
        if self.open_gate is not None and not self.open_gate.wait(2):
            raise AssertionError("Test did not release the fake opening gate")
        ctypes.cast(destination, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(42)
        return 0

    def waveInPrepareHeader(self, *_args):
        self.operations.append("prepare")
        return 0

    def waveInAddBuffer(self, _device, header, _size):
        self.operations.append("add")
        self.header = ctypes.cast(header, ctypes.POINTER(audio._WaveHeader)).contents
        return 0

    def waveInStart(self, _device):
        self.operations.append("start")
        pcm = b"\x01\x00" * 64
        ctypes.memmove(self.header.lpData, pcm, len(pcm))
        self.header.dwBytesRecorded = len(pcm)
        self.header.dwFlags |= audio._WHDR_DONE
        return 0

    def waveInReset(self, _device):
        self.operations.append("reset")
        if self.header is not None:
            self.header.dwFlags |= audio._WHDR_DONE
        return 0

    def waveInUnprepareHeader(self, *_args):
        self.operations.append("unprepare")
        return 33 if self.cleanup_failure else 0

    def waveInClose(self, _device):
        self.operations.append("close")
        return 33 if self.cleanup_failure else 0


class AudioTests(unittest.TestCase):
    def setUp(self):
        retained = patch.object(audio, "_unreleased_sessions", [])
        retained.start()
        self.addCleanup(retained.stop)

    def test_wav_header_and_even_pcm_alignment(self):
        wav = audio._wav_bytes(b"\x01\x00\xff")
        header = struct.unpack("<4sI4s4sIHHIIHH4sI", wav[:44])
        self.assertEqual((b"RIFF", 38, b"WAVE", b"fmt ", 16, 1, 1,
                          16_000, 32_000, 2, 16, b"data", 2), header)
        self.assertEqual(b"\x01\x00", wav[44:])

    def test_fake_capture_releases_native_resources(self):
        native, kernel = FakeWinMM(), FakeKernel()
        with patch.object(audio, "_winmm", native), patch.object(audio, "_kernel32", kernel):
            recorder = audio.WaveRecorder()
            wav = recorder.start(1).result(timeout=2)
            self.assertEqual(b"RIFF", wav[:4])
            self.assertEqual(172, len(wav))
            self.assertEqual(b"\x01\x00" * 64, wav[44:])
            self.assertFalse(recorder.is_recording)
            self.assertIsNone(recorder._current)
            self.assertEqual(["open", "prepare", "add", "start", "reset", "unprepare", "close"], native.operations)
            self.assertEqual([7], kernel.closed)

    def test_cancel_during_open_is_nonblocking_and_never_starts_capture(self):
        gate = threading.Event()
        native, kernel = FakeWinMM(open_gate=gate), FakeKernel()
        with patch.object(audio, "_winmm", native), patch.object(audio, "_kernel32", kernel):
            recorder = audio.WaveRecorder()
            before = time.monotonic()
            completion = recorder.start(1)
            self.assertLess(time.monotonic() - before, 0.5)
            try:
                self.assertTrue(native.entered.wait(1))
                before = time.monotonic()
                self.assertIs(completion, recorder.cancel())
                self.assertLess(time.monotonic() - before, 0.5)
            finally:
                gate.set()
            self.assertEqual(b"", completion.result(timeout=2))
            self.assertNotIn("start", native.operations)
            self.assertIsNone(recorder._current)

    def test_cleanup_failure_retains_driver_owned_buffers(self):
        gate = threading.Event()
        native, kernel = FakeWinMM(open_gate=gate, cleanup_failure=True), FakeKernel()
        with patch.object(audio, "_winmm", native), patch.object(audio, "_kernel32", kernel):
            recorder = audio.WaveRecorder()
            completion = recorder.start(1)
            try:
                self.assertTrue(native.entered.wait(1))
                recorder.cancel()
            finally:
                gate.set()
            with self.assertRaises(OSError):
                completion.result(timeout=2)
            self.assertIsNotNone(recorder._current)
            self.assertIsNotNone(recorder._current.audio)
            self.assertTrue(recorder._current.prepared)
            self.assertEqual([], kernel.closed)
            with self.assertRaises(RuntimeError):
                recorder.start(1)

    def test_future_callback_can_inspect_recorder_without_deadlock(self):
        gate = threading.Event()
        native, kernel = FakeWinMM(open_gate=gate), FakeKernel()
        with patch.object(audio, "_winmm", native), patch.object(audio, "_kernel32", kernel):
            recorder = audio.WaveRecorder()
            completion = recorder.start(1)
            callback_done = threading.Event()
            observed = []

            def callback(_future):
                observed.append(recorder.is_recording)
                callback_done.set()

            completion.add_done_callback(callback)
            gate.set()
            completion.result(timeout=2)
            self.assertTrue(callback_done.wait(1))
            self.assertEqual([False], observed)

    def test_invalid_recording_limits_are_rejected_without_native_calls(self):
        native = FakeWinMM()
        with patch.object(audio, "_winmm", native), patch.object(audio, "_kernel32", FakeKernel()):
            for limit in (0, 31, True, 1.5):
                with self.subTest(limit=limit), self.assertRaises(ValueError):
                    audio.WaveRecorder().start(limit)
        self.assertEqual([], native.operations)

    def test_worker_start_failure_does_not_block_the_next_recording(self):
        native, kernel = FakeWinMM(), FakeKernel()
        with patch.object(audio, "_winmm", native), patch.object(audio, "_kernel32", kernel):
            recorder = audio.WaveRecorder()
            with patch("envi.audio.threading.Thread.start", side_effect=RuntimeError("Fake thread failure")):
                with self.assertRaises(RuntimeError):
                    recorder.start(1)
            self.assertIsNone(recorder._current)
            self.assertFalse(recorder.is_recording)
            self.assertEqual([], native.operations)
            self.assertEqual(b"RIFF", recorder.start(1).result(timeout=2)[:4])

    def test_failed_cleanup_retains_buffers_after_recorder_is_collected(self):
        gate = threading.Event()
        native, kernel = FakeWinMM(open_gate=gate, cleanup_failure=True), FakeKernel()
        with patch.object(audio, "_winmm", native), patch.object(audio, "_kernel32", kernel):
            recorder = audio.WaveRecorder()
            completion = recorder.start(1)
            try:
                self.assertTrue(native.entered.wait(1))
                recorder.cancel()
            finally:
                gate.set()
            with self.assertRaises(OSError):
                completion.result(timeout=2)
            session_ref = weakref.ref(recorder._current)
            del recorder, completion
            gc.collect()
            self.assertIsNotNone(session_ref())
            self.assertIn(session_ref(), audio._unreleased_sessions)
            self.assertEqual([], kernel.closed)


class FakeRecorder:
    def __init__(self):
        self.current = None
        self.maximum = None

    @property
    def is_recording(self):
        return self.current is not None and not self.current.done()

    def start(self, maximum_seconds):
        self.maximum = maximum_seconds
        self.current = Future()
        return self.current

    def finish(self, wav):
        self.current.set_result(wav)

    def stop(self):
        if self.is_recording:
            self.finish(audio._wav_bytes(b"\x01\x00" * 64))
        return self.current

    def cancel(self):
        if self.is_recording:
            self.finish(b"")
        return self.current

    def close(self):
        return self.cancel()


class FakeGroq:
    def __init__(self):
        self.session_api_requests = 0
        self.transcriptions = []

    def transcribe(self, wav, token):
        token.check()
        self.transcriptions.append(wav)
        return "Открой блокнот"


class FakeAssistant:
    def __init__(self):
        self.calls = []
        self.mode = "local"
        self.finished = threading.Event()
        self.approved = False

    def process(self, text, confirm, token):
        self.calls.append(text)
        try:
            if self.mode == "partial":
                token.cancel()
                return "Запрос отменён. Уже выполнено: проверочное действие; оно не откатывается."
            if self.mode == "confirmation":
                self.approved = confirm(ToolCall("open_app", '{"app":"notepad"}', "fake"))
                return "Разрешено." if self.approved else "Действие отменено."
            return "Сейчас 12:00"
        finally:
            self.finished.set()

    def clear_history(self):
        self.calls.clear()


@unittest.skipIf(tk is None, "tkinter is unavailable; core/audio tests remain usable")
class DesktopTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest("No usable Tk display; hidden desktop tests are skipped")
        self.root.withdraw()
        self.recorder = FakeRecorder()
        self.groq = FakeGroq()
        self.assistant = FakeAssistant()
        self.tools = SimpleNamespace(describe=lambda _call: "Блокнот: проверочная разрешённая цель")
        with patch("envi.ui.WaveRecorder", return_value=self.recorder):
            self.window = EnviWindow(self.root, AppSettings(), self.groq, self.assistant, self.tools, True)
        self.addCleanup(self._close)

    def _close(self):
        if not self.window._closing:
            self.window.close()

    def wait_until(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.005)
        self.assertTrue(predicate(), "Hidden UI did not finish the expected operation")

    def send(self, text):
        self.window.input_box.insert("1.0", text)
        self.window._send()

    def hidden_dialog(self, *args, **kwargs):
        # A real Tk dialog verifies queue/confirmation widgets, but is never
        # mapped on the user's desktop. Its modal grab is unnecessary here.
        dialog = self.original_toplevel(*args, **kwargs)
        dialog.withdraw()
        dialog.grab_set = lambda: None
        return dialog

    def begin_confirmation(self):
        self.assistant.mode = "confirmation"
        self.original_toplevel = tk.Toplevel
        with patch("envi.ui.tk.Toplevel", side_effect=self.hidden_dialog):
            self.send("Открой блокнот")
            self.wait_until(lambda: self.window._dialog_request is not None)
        return self.window._dialog_request

    def test_missing_key_disables_recording_but_local_text_works(self):
        self.window.has_api_key = False
        self.window._refresh_controls()
        self.assertEqual("disabled", str(self.window.record_button["state"]))
        self.send("Который час?")
        self.wait_until(lambda: self.window._kind is None)
        self.assertIn("Сейчас 12:00", self.window.history_box.get("1.0", "end"))
        self.assertEqual(["Который час?"], self.assistant.calls)
        self.assertEqual([], self.groq.transcriptions)

    def test_partial_action_answer_survives_cancellation(self):
        self.assistant.mode = "partial"
        self.send("Проверочный запрос")
        self.wait_until(lambda: self.window._kind is None)
        self.assertIn("Уже выполнено", self.window.history_box.get("1.0", "end"))

    def test_transcript_requires_review_and_is_never_automatically_processed(self):
        self.window._record()
        self.assertEqual(30, self.recorder.maximum)
        self.recorder.finish(audio._wav_bytes(b"\x01\x00" * 64))
        self.wait_until(lambda: self.window._kind is None)
        self.assertEqual("Открой блокнот", self.window.input_box.get("1.0", "end-1c"))
        self.assertIn("Проверьте текст", self.window.status.get())
        self.assertEqual([], self.assistant.calls)
        self.assertEqual(1, len(self.groq.transcriptions))

    def test_cancel_recording_never_calls_stt(self):
        self.window._record()
        self.window.cancel()
        self.wait_until(lambda: self.window._kind is None)
        self.assertEqual([], self.groq.transcriptions)
        self.assertEqual([], self.assistant.calls)

    def test_pending_confirmation_is_resolved_on_cancel(self):
        request = self.begin_confirmation()
        self.window.cancel()
        self.assertTrue(request.completed.is_set())
        self.assertFalse(request.approved)
        self.wait_until(lambda: self.window._kind is None)
        self.assertTrue(self.assistant.finished.is_set())
        self.assertFalse(self.assistant.approved)
        self.assertIsNone(self.window._dialog)

    def test_pending_confirmation_is_resolved_on_close(self):
        request = self.begin_confirmation()
        self.window.close()
        self.assertTrue(request.completed.is_set())
        self.assertFalse(request.approved)
        self.assertTrue(self.assistant.finished.wait(1))
        self.assertFalse(self.assistant.approved)
        self.assertTrue(self.window._closing)

    def test_history_widgets_are_bounded(self):
        # Hidden widgets are only one pixel wide until mapped. Explicit newlines
        # keep this a memory-boundary test, not a pathological Tk layout test.
        self.window._add_history("Вы", "x\n" * 40_000)
        self.window._add_history("Envi", "y\n" * 40_000)
        self.window._add_history("Вы", "Новый запрос после сокращения истории")
        history = self.window.history_box.get("1.0", "end-1c")
        self.assertLess(len(history), 100_000)
        self.assertEqual(1, history.count("предыдущие сообщения скрыты"))
        self.assertIn("Новый запрос после сокращения истории", history)

    def test_history_appends_without_rebuilding_existing_messages(self):
        with patch.object(self.window.history_box, "delete", wraps=self.window.history_box.delete) as delete:
            self.window._add_history("Вы", "Первый запрос")
            self.window._add_history("Envi", "Первый ответ")
            delete.assert_not_called()
        history = self.window.history_box.get("1.0", "end-1c")
        self.assertEqual(1, history.count("Первый запрос"))
        self.assertEqual(1, history.count("Первый ответ"))
        self.assertEqual(self.window._history_size, len(history))

    def test_worker_start_failure_restores_controls_and_allows_retry(self):
        with patch("envi.ui.threading.Thread.start", side_effect=RuntimeError("Fake thread failure")):
            self.send("Первый запрос")
        self.wait_until(lambda: self.window._kind is None)
        self.assertEqual([], self.assistant.calls)
        self.assertEqual("normal", str(self.window.send_button["state"]))
        self.assertIn("Не удалось запустить обработку", self.window.history_box.get("1.0", "end"))
        self.send("Который час?")
        self.wait_until(lambda: self.window._kind is None)
        self.assertEqual(["Который час?"], self.assistant.calls)

    def test_confirmation_creation_failure_denies_action_and_keeps_polling(self):
        self.assistant.mode = "confirmation"
        with patch("envi.ui.tk.Toplevel", side_effect=tk.TclError("Fake dialog failure")):
            self.send("Открой блокнот")
            self.wait_until(lambda: self.window._kind is None)
        self.assertFalse(self.assistant.approved)
        self.assertTrue(self.assistant.finished.is_set())
        self.assertIsNone(self.window._dialog)
        self.assertIn("Действие не выполнено", self.window.history_box.get("1.0", "end"))
        self.assertIsNotNone(self.window._after_id)

    def test_confirmation_setup_failure_closes_partial_dialog(self):
        self.assistant.mode = "confirmation"
        self.original_toplevel = tk.Toplevel
        dialogs = []

        def fail_grab():
            raise tk.TclError("Fake grab failure")

        def failing_dialog(*args, **kwargs):
            dialog = self.hidden_dialog(*args, **kwargs)
            dialog.grab_set = fail_grab
            dialog.grab_release = fail_grab
            dialogs.append(dialog)
            return dialog

        with patch("envi.ui.tk.Toplevel", side_effect=failing_dialog):
            self.send("Открой блокнот")
            self.wait_until(lambda: self.window._kind is None)
        self.assertFalse(self.assistant.approved)
        self.assertIsNone(self.window._dialog)
        self.assertIsNone(self.window._dialog_request)
        self.assertFalse(dialogs[0].winfo_exists())

    def test_worker_cannot_directly_mutate_tk_status(self):
        failures = []

        def worker():
            try:
                self.window._set_status("Must not reach Tcl")
            except RuntimeError as error:
                failures.append(error)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(failures))


if __name__ == "__main__":
    unittest.main()
