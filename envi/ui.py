"""Tk desktop shell. Workers communicate through a queue; only the main thread touches Tk."""

from __future__ import annotations

import threading
import tkinter as tk
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime
from queue import Empty, Queue
from tkinter import ttk
from typing import Any, Callable

from .audio import WaveRecorder
from .cancellation import CancellationToken
from .configuration import AppSettings
from .errors import AssistantError, CancelledError
from .models import ToolCall, Tools


@dataclass(eq=False)
class _ConfirmationRequest:
    call: ToolCall
    completed: threading.Event = field(default_factory=threading.Event)
    approved: bool = False
    error: Exception | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def resolve(self, approved: bool = False, error: Exception | None = None) -> None:
        with self.lock:
            if not self.completed.is_set():
                self.approved = approved
                self.error = error
                self.completed.set()


class EnviWindow:
    def __init__(self, root: tk.Tk, settings: AppSettings, groq: Any,
                 assistant: Any, tools: Tools, has_api_key: bool) -> None:
        self.root = root
        self.settings = settings
        self.groq = groq
        self.assistant = assistant
        self.tools = tools
        self.has_api_key = has_api_key
        self.recorder = WaveRecorder()
        self._queue: Queue[tuple[str, int, Any]] = Queue()
        self._pending_lock = threading.Lock()
        self._pending: set[_ConfirmationRequest] = set()
        self._dialog: tk.Toplevel | None = None
        self._dialog_request: _ConfirmationRequest | None = None
        self._token: CancellationToken | None = None
        self._kind: str | None = None
        self._generation = 0
        self._record_stopping = False
        self._closing = False
        self._history: deque[str] = deque()
        self._history_size = 0
        self._history_truncated = False
        self._after_id: str | None = None
        self._main_thread = threading.get_ident()

        root.title("Envi — Python")
        root.geometry("900x620")
        root.minsize(620, 440)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        frame = ttk.Frame(root, padding=20)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        heading = ttk.Frame(frame)
        heading.grid(row=0, column=0, sticky="ew")
        heading.columnconfigure(0, weight=1)
        ttk.Label(heading, text="Envi", font=("Segoe UI", 24, "bold")).grid(row=0, column=0, sticky="w")
        self.api_count = tk.StringVar(root)
        ttk.Label(heading, textvariable=self.api_count).grid(row=0, column=1, sticky="e")
        self.status = tk.StringVar(root)
        ttk.Label(frame, textvariable=self.status, wraplength=800).grid(
            row=1, column=0, sticky="ew", pady=(4, 14))

        history_frame = ttk.Frame(frame)
        history_frame.grid(row=2, column=0, sticky="nsew")
        history_frame.columnconfigure(0, weight=1)
        history_frame.rowconfigure(0, weight=1)
        self.history_box = tk.Text(history_frame, wrap="word", state="disabled",
                                   font=("Segoe UI", 11), padx=12, pady=12)
        self.history_box.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(history_frame, orient="vertical", command=self.history_box.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.history_box.configure(yscrollcommand=scrollbar.set)

        self.input_box = tk.Text(frame, height=4, wrap="word", font=("Segoe UI", 11), padx=10, pady=10)
        self.input_box.grid(row=3, column=0, sticky="ew", pady=(14, 10))
        self.input_box.bind("<Control-Return>", self._send_key)
        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, sticky="e")
        self.record_button = ttk.Button(buttons, text="Записать речь", command=self._record)
        self.send_button = ttk.Button(buttons, text="Отправить", command=self._send)
        self.cancel_button = ttk.Button(buttons, text="Отмена", command=self.cancel)
        self.clear_button = ttk.Button(buttons, text="Очистить окно", command=self._clear)
        for column, button in enumerate((self.record_button, self.send_button, self.cancel_button, self.clear_button)):
            button.grid(row=0, column=column, padx=(0, 8 if column < 3 else 0))
        self._set_status(
            "Готово. Ctrl+Enter — отправить. Распознанную речь нужно проверить перед отправкой."
            if has_api_key else
            "GROQ_API_KEY не задан. Локальные текстовые команды доступны; облачные ответы и речь — нет.")
        self._refresh_controls()
        self.input_box.focus_set()
        self._after_id = root.after(50, self._poll)

    def _assert_main_thread(self) -> None:
        if threading.get_ident() != self._main_thread:
            raise RuntimeError("Tk UI must only be accessed from its main thread.")

    def _set_status(self, text: str) -> None:
        self._assert_main_thread()
        self.status.set(text)

    def _refresh_controls(self) -> None:
        self._assert_main_thread()
        busy = self._kind is not None
        recording = self._kind == "audio" and self.recorder.is_recording
        self.record_button.configure(text="Остановить запись" if recording else "Записать речь")
        self.record_button.configure(state="normal" if self.has_api_key and
                                     (not busy or recording and not self._record_stopping) else "disabled")
        self.send_button.configure(state="disabled" if busy else "normal")
        self.input_box.configure(state="disabled" if busy else "normal")
        self.cancel_button.configure(state="normal" if busy and self._token and
                                     not self._token.is_cancelled else "disabled")
        self.clear_button.configure(state="disabled" if busy else "normal")
        self.api_count.set(f"API-запросов: {self.groq.session_api_requests}/{self.settings.max_session_api_requests}")

    def _add_history(self, speaker: str, message: str) -> None:
        self._assert_main_thread()
        entry = f"{datetime.now():%H:%M}  {speaker}: {message[:80_000]}\n\n"
        self._history.append(entry)
        self._history_size += len(entry)
        if self._history_size > 100_000:
            while len(self._history) > 1 and self._history_size > 80_000:
                self._history_size -= len(self._history.popleft())
            self._history_truncated = True
        text = ("…предыдущие сообщения скрыты…\n\n" if self._history_truncated else "") + "".join(self._history)
        self.history_box.configure(state="normal")
        self.history_box.delete("1.0", "end")
        self.history_box.insert("1.0", text)
        self.history_box.configure(state="disabled")
        self.history_box.see("end")

    def _begin(self, kind: str) -> tuple[int, CancellationToken]:
        self._generation += 1
        self._kind = kind
        self._token = CancellationToken()
        self._record_stopping = False
        self._refresh_controls()
        return self._generation, self._token

    def _finish(self) -> None:
        self._kind = None
        self._token = None
        self._record_stopping = False
        self._refresh_controls()
        self.input_box.focus_set()

    def _send_key(self, _event: Any) -> str:
        self._send()
        return "break"

    def _send(self) -> None:
        self._assert_main_thread()
        if self._closing or self._kind is not None:
            return
        text = self.input_box.get("1.0", "end-1c").strip()
        if not text:
            return
        if len(text) > 4000:
            self._set_status("Запрос слишком длинный: сократите его до 4000 символов.")
            return
        self.input_box.delete("1.0", "end")
        self._add_history("Вы", text)
        generation, token = self._begin("chat")
        self._set_status("Обрабатываю запрос…")
        self._start_worker(generation, token, "answer", lambda: self.assistant.process(
            text, lambda call: self._confirm_from_worker(call, generation, token), token))

    def _start_worker(self, generation: int, token: CancellationToken,
                      success_kind: str, work: Callable[[], str]) -> None:
        def run() -> None:
            try:
                token.check()
                result = work()
                if success_kind == "transcript":
                    token.check()
                # The core can return an honest partial-action report even
                # after cancellation. Do not discard that successful answer.
                self._queue.put((success_kind, generation, result))
            except CancelledError:
                self._queue.put(("cancelled", generation, None))
            except AssistantError as error:
                self._queue.put(("error", generation, str(error)))
            except Exception:
                # Raw exceptions may contain provider responses or credentials.
                self._queue.put(("error", generation, "Не удалось выполнить запрос. Проверьте настройки и повторите попытку."))
        threading.Thread(target=run, name="EnviRequest", daemon=True).start()

    def _record(self) -> None:
        self._assert_main_thread()
        if self._closing or not self.has_api_key:
            return
        if self._kind == "audio" and self.recorder.is_recording and not self._record_stopping:
            self._record_stopping = True
            self.recorder.stop()
            self._set_status("Останавливаю запись…")
            self._refresh_controls()
            return
        if self._kind is not None:
            return
        generation, _token = self._begin("audio")
        try:
            completion = self.recorder.start(self.settings.recording_max_seconds)
            completion.add_done_callback(lambda done: self._queue.put(("recorded", generation, done)))
            self._set_status(f"Идёт запись (не более {self.settings.recording_max_seconds} с). Нажмите кнопку ещё раз для остановки.")
            self._refresh_controls()
        except Exception:
            self._set_status("Микрофон недоступен. Проверьте устройство и разрешение Windows на запись звука.")
            self._finish()

    def _handle_recorded(self, generation: int, completion: Future[bytes]) -> None:
        if self._token is None:
            return
        if self._token.is_cancelled:
            self._set_status("Запись отменена.")
            self._finish()
            return
        try:
            wav = completion.result()
        except Exception:
            self._set_status("Не удалось записать звук. Проверьте микрофон и разрешения Windows.")
            self._finish()
            return
        if len(wav) <= 44:
            self._set_status("Запись пуста. Попробуйте ещё раз.")
            self._finish()
            return
        self._record_stopping = True
        self._set_status("Распознаю речь через Groq…")
        self._refresh_controls()
        token = self._token
        self._start_worker(generation, token, "transcript", lambda: self.groq.transcribe(wav, token))

    def _confirm_from_worker(self, call: ToolCall, generation: int, token: CancellationToken) -> bool:
        token.check()
        request = _ConfirmationRequest(call)
        with self._pending_lock:
            self._pending.add(request)
        unregister = token.register(request.resolve)
        self._queue.put(("confirmation", generation, request))
        try:
            # Cancellation resolves the Event without any worker-side Tk call.
            while not request.completed.wait(0.1):
                token.check()
            token.check()
            if request.error is not None:
                raise request.error
            return request.approved
        finally:
            unregister()
            with self._pending_lock:
                self._pending.discard(request)

    def _show_confirmation(self, request: _ConfirmationRequest) -> None:
        if request.completed.is_set() or self._closing or self._token is None or self._token.is_cancelled:
            request.resolve()
            return
        if self._dialog is not None:
            request.resolve()  # Never allow overlapping confirmations.
            return
        try:
            target = self.tools.describe(request.call)
        except AssistantError as error:
            request.resolve(error=error)
            return
        except Exception:
            request.resolve(error=AssistantError("Не удалось проверить цель действия."))
            return
        dialog = tk.Toplevel(self.root)
        self._dialog = dialog
        self._dialog_request = request
        dialog.title("Подтверждение действия")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        box = ttk.Frame(dialog, padding=20)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="Разрешить Envi выполнить действие?", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        target_box = tk.Text(box, width=68, height=6, wrap="word", font=("Segoe UI", 10))
        target_box.pack(fill="both", pady=12)
        target_box.insert("1.0", f"{target}\n\nИнструмент: {request.call.name}")
        target_box.configure(state="disabled")
        row = ttk.Frame(box)
        row.pack(anchor="e")
        ttk.Button(row, text="Разрешить", command=lambda: self._finish_confirmation(True)).pack(side="left", padx=(0, 8))
        deny = ttk.Button(row, text="Не разрешать", command=lambda: self._finish_confirmation(False))
        deny.pack(side="left")
        dialog.protocol("WM_DELETE_WINDOW", lambda: self._finish_confirmation(False))
        dialog.bind("<Escape>", lambda _event: self._finish_confirmation(False))
        dialog.bind("<Return>", lambda _event: self._finish_confirmation(False))
        dialog.grab_set()
        deny.focus_set()

    def _finish_confirmation(self, approved: bool = False) -> None:
        self._assert_main_thread()
        if self._dialog_request is not None:
            self._dialog_request.resolve(approved)
        self._dialog_request = None
        if self._dialog is not None:
            try:
                self._dialog.grab_release()
                self._dialog.destroy()
            except tk.TclError:
                pass
            self._dialog = None

    def _drop_confirmations(self) -> None:
        with self._pending_lock:
            pending = tuple(self._pending)
        for request in pending:
            request.resolve()
        self._finish_confirmation(False)

    def cancel(self) -> None:
        self._assert_main_thread()
        if self._token is not None:
            self._token.cancel()
            self._drop_confirmations()
            if self._kind == "audio":
                self._record_stopping = True
                self.recorder.cancel()
            if not self._closing:
                self._set_status("Отменяю запрос…")
                self._refresh_controls()

    def _clear(self) -> None:
        if self._closing or self._kind is not None:
            return
        self.assistant.clear_history()
        self._history.clear()
        self._history_size = 0
        self._history_truncated = False
        self.history_box.configure(state="normal")
        self.history_box.delete("1.0", "end")
        self.history_box.configure(state="disabled")
        self._set_status("История текущего диалога очищена. Технический журнал на диске не удалялся.")

    def _poll(self) -> None:
        self._assert_main_thread()
        self._after_id = None
        if self._closing:
            return
        if self._dialog_request is not None and self._dialog_request.completed.is_set():
            self._finish_confirmation(False)
        for _ in range(50):
            try:
                kind, generation, data = self._queue.get_nowait()
            except Empty:
                break
            if generation != self._generation or self._kind is None:
                if kind == "confirmation":
                    data.resolve()
                continue
            if kind == "confirmation":
                self._show_confirmation(data)
                continue
            if kind == "recorded":
                self._handle_recorded(generation, data)
                continue
            if kind != "answer" and self._token is not None and self._token.is_cancelled:
                kind = "cancelled"
            if kind == "answer":
                self._add_history("Envi", data)
                self._set_status("Готово.")
            elif kind == "transcript":
                transcript = data.strip()
                self._finish()
                if transcript:
                    previous = self.input_box.get("1.0", "end-1c").rstrip()
                    self.input_box.delete("1.0", "end")
                    self.input_box.insert("1.0", f"{previous} {transcript}" if previous else transcript)
                    self.input_box.mark_set("insert", "end-1c")
                    self.input_box.see("end")
                    self._set_status("Речь распознана. Проверьте текст и нажмите «Отправить».")
                else:
                    self._set_status("Речь не распознана. Попробуйте ещё раз.")
                continue
            elif kind == "cancelled":
                self._set_status("Запрос отменён.")
            elif kind == "error":
                self._add_history("Envi", data)
                self._set_status("Запрос не выполнен.")
            self._finish()
        self._refresh_controls()
        self._after_id = self.root.after(50, self._poll)

    def close(self) -> None:
        """Cancel and close immediately; daemon workers never access a destroyed Tk interpreter."""
        self._assert_main_thread()
        if self._closing:
            return
        self._closing = True
        self.cancel()
        self._drop_confirmations()
        self.recorder.close()
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        self.root.destroy()
