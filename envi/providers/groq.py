"""Explicit REST adapter, no SDK dependency, redirects, background retries or credential logging."""

import http.client
import io
import json
import socket
import ssl
import time
import uuid
import wave
from collections.abc import Sequence
from dataclasses import dataclass, field
from threading import Event, Lock, Timer
from typing import Any, Protocol
from urllib.parse import urlsplit

from ..cancellation import CancellationToken
from ..configuration import AppSettings
from ..errors import AssistantError
from ..json_utils import strict_json_loads
from ..models import ChatMessage, ModelReply, ToolCall

_BASE_URL = "https://api.groq.com/openai/v1/"
_MAX_RESPONSE_BYTES = 1_048_576


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


class HttpTransport(Protocol):
    def send(self, url: str, headers: dict[str, str], bodybytes: bytes,
             timeout_seconds: int, token: CancellationToken) -> HttpResponse: ...


class StandardHttpTransport:
    def send(self, url: str, headers: dict[str, str], bodybytes: bytes,
             timeout_seconds: int, token: CancellationToken) -> HttpResponse:
        target = urlsplit(url)
        if target.scheme != "https" or target.netloc != "api.groq.com" or target.query or target.fragment or \
                target.path not in ("/openai/v1/chat/completions", "/openai/v1/audio/transcriptions"):
            raise AssistantError("Неподдерживаемый адрес API. Ключ никуда не отправлен.")
        token.check()
        deadline = time.monotonic() + timeout_seconds
        connection = http.client.HTTPSConnection("api.groq.com", timeout=timeout_seconds,
                                                 context=ssl.create_default_context())
        connection.auto_open = False
        active_socket: list[socket.socket | None] = [None]
        deadline_expired = Event()
        response: http.client.HTTPResponse | None = None

        def check_deadline() -> float:
            token.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0 or deadline_expired.is_set():
                raise TimeoutError()
            return remaining

        def prepare_io() -> None:
            remaining = check_deadline()
            if active_socket[0] is not None:
                active_socket[0].settimeout(remaining)

        def interrupt() -> None:
            current_socket = active_socket[0]
            if current_socket is not None:
                try:
                    current_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()

        def expire() -> None:
            deadline_expired.set()
            interrupt()

        unregister = token.register(interrupt)
        timer = Timer(timeout_seconds, expire)
        timer.daemon = True
        try:
            timer.start()
            check_deadline()
            connection.connect()
            active_socket[0] = connection.sock
            prepare_io()
            connection.request("POST", target.path, body=bodybytes, headers=headers)
            prepare_io()
            response = connection.getresponse()
            chunks = bytearray()
            while True:
                prepare_io()
                chunk = response.read1(8192)
                check_deadline()
                if not chunk:
                    break
                if len(chunks) + len(chunk) > _MAX_RESPONSE_BYTES:
                    raise AssistantError("Ответ Groq превышает допустимый размер.")
                chunks.extend(chunk)
            return HttpResponse(response.status, bytes(chunks), dict(response.getheaders()))
        except (TimeoutError, socket.timeout) as error:
            token.check()
            raise AssistantError("Groq не ответил вовремя. Автоповтора нет; попробуй вручную позже.") from error
        except (OSError, http.client.HTTPException) as error:
            token.check()
            if deadline_expired.is_set():
                raise AssistantError("Groq не ответил вовремя. Автоповтора нет; попробуй вручную позже.") from error
            raise AssistantError("Не удалось соединиться с Groq. Проверь интернет. Локальные текстовые команды доступны.") from error
        finally:
            timer.cancel()
            unregister()
            try:
                if response is not None:
                    response.close()
            finally:
                connection.close()


class GroqClient:
    def __init__(self, settings: AppSettings, key: str | None, transport: HttpTransport | None = None) -> None:
        self.settings = settings
        self._key = key.strip() if key else ""
        self._transport = transport if transport is not None else StandardHttpTransport()
        self._quota_lock = Lock()
        self._requests = 0

    @property
    def session_api_requests(self) -> int:
        with self._quota_lock:
            return self._requests

    def complete(self, messages: Sequence[ChatMessage], schemas: Sequence[dict[str, Any]],
                 token: CancellationToken) -> ModelReply:
        token.check()
        body: dict[str, Any] = {
            "model": self.settings.chat_model,
            "messages": [self._wire_message(message) for message in messages],
            "max_completion_tokens": self.settings.max_output_tokens, "temperature": 0.2,
        }
        if schemas:
            body.update(tools=list(schemas), tool_choice="auto")
        if self.settings.chat_model in ("openai/gpt-oss-20b", "openai/gpt-oss-120b"):
            body["reasoning_effort"] = "low"
        response = self._send("chat/completions", json.dumps(body, ensure_ascii=False).encode("utf-8"),
                              "application/json", token)
        return self._parse_reply(response)

    @staticmethod
    def _parse_reply(response: bytes) -> ModelReply:
        try:
            root = strict_json_loads(response.decode("utf-8"))
            if not isinstance(root, dict):
                raise ValueError()
            choices = root.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ValueError()
            choice = choices[0]
            if not isinstance(choice, dict):
                raise ValueError()
            message = choice.get("message")
            if not isinstance(message, dict):
                raise ValueError()
            content = message.get("content")
            finish_reason = choice.get("finish_reason")
            if content is not None and not isinstance(content, str):
                raise ValueError()
            if finish_reason is not None and not isinstance(finish_reason, str):
                raise ValueError()
            wire_calls = message.get("tool_calls")
            if wire_calls is None:
                wire_calls = []
            if not isinstance(wire_calls, list):
                raise ValueError()
            calls = []
            for call in wire_calls:
                if not isinstance(call, dict):
                    raise ValueError()
                function = call.get("function")
                if not isinstance(function, dict):
                    raise ValueError()
                if call["type"] != "function" or any(not isinstance(value, str) for value in
                                                      (call["id"], function["name"], function["arguments"])):
                    raise ValueError()
                calls.append(ToolCall(function["name"], function["arguments"], call["id"]))
            return ModelReply(content, tuple(calls), finish_reason)
        except (UnicodeError, ValueError, KeyError, IndexError, TypeError, AttributeError, RecursionError) as error:
            raise AssistantError("Groq вернул ответ неожиданного формата. Действия из этого ответа не выполнены.") from error

    def transcribe(self, wavbytes: bytes, token: CancellationToken) -> str:
        token.check()
        self._validate_wav(wavbytes)
        boundary = "envi-" + uuid.uuid4().hex
        pieces: list[bytes] = []
        for name, value in (("model", self.settings.stt_model), ("language", self.settings.language),
                            ("response_format", "text"), ("temperature", "0")):
            pieces.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                           f'{value}\r\n').encode("utf-8"))
        pieces.extend((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
                       f'Content-Type: audio/wav\r\n\r\n'.encode("ascii"), wavbytes,
                       f'\r\n--{boundary}--\r\n'.encode("ascii")))
        body = self._send("audio/transcriptions", b"".join(pieces), f"multipart/form-data; boundary={boundary}", token)
        try:
            text = body.decode("utf-8").strip()
        except UnicodeError as error:
            raise AssistantError("Groq вернул некорректный текст распознавания.") from error
        if not text:
            raise AssistantError("Речь не распознана. Попробуй записать фразу ещё раз.")
        if len(text) > 4000:
            raise AssistantError("Распознанный текст слишком длинный для одного запроса.")
        return text

    def _send(self, endpoint: str, body: bytes, content_type: str, token: CancellationToken) -> bytes:
        token.check()
        if not self._key:
            raise AssistantError("Нет GROQ_API_KEY. Добавь ключ в переменную окружения и перезапусти Envi. Текстовые локальные команды работают без ключа.")
        if not self._key.isascii() or any(ord(char) < 33 or ord(char) > 126 for char in self._key):
            raise AssistantError("Некорректный формат GROQ_API_KEY. Проверь переменную окружения.")
        with self._quota_lock:
            if self._requests >= self.settings.max_session_api_requests:
                raise AssistantError("Достигнут локальный лимит API-запросов сессии. Это счётчик запросов, не денежный бюджет. Автоповтора нет.")
            self._requests += 1
        response = self._transport.send(_BASE_URL + endpoint,
                                        {"Authorization": "Bearer " + self._key, "Content-Type": content_type},
                                        body, self.settings.timeout_seconds, token)
        token.check()
        if not 200 <= response.status < 300:
            errors = {
                401: "Groq не принял ключ. Проверь GROQ_API_KEY.",
                403: "Groq запретил запрос. Проверь доступ к модели в аккаунте.",
                429: "Достигнут лимит Groq (429). Подожди или проверь квоту в кабинете. Автоповтора нет.",
                400: "Groq отклонил запрос. Проверь выбранную модель и её доступность.",
                404: "Groq не нашёл модель или endpoint. Проверь настройки и доступность модели.",
            }
            raise AssistantError(errors.get(response.status, f"Groq недоступен (HTTP {response.status}). Повтори вручную позже."))
        if len(response.body) > _MAX_RESPONSE_BYTES:
            raise AssistantError("Ответ Groq превышает допустимый размер.")
        return response.body

    def _validate_wav(self, data: bytes) -> None:
        if not isinstance(data, bytes) or not 44 < len(data) <= 44 + 32_000 * self.settings.recording_max_seconds:
            raise AssistantError("Запись пустая или превышает разрешённую длительность.")
        try:
            with wave.open(io.BytesIO(data), "rb") as recording:
                frames = recording.getnframes()
                if recording.getnchannels() != 1 or recording.getsampwidth() != 2 or \
                        recording.getframerate() != 16_000 or recording.getcomptype() != "NONE" or \
                        not 0 < frames <= 16_000 * self.settings.recording_max_seconds or \
                        len(recording.readframes(frames)) != frames * 2:
                    raise ValueError()
        except (wave.Error, EOFError, ValueError) as error:
            raise AssistantError("Нужна корректная запись WAV: 16 kHz, 16-bit, mono PCM.") from error

    @staticmethod
    def _wire_message(message: ChatMessage) -> dict[str, Any]:
        wire: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_call_id is not None:
            wire["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            wire["tool_calls"] = [{"id": call.id, "type": "function", "function":
                                   {"name": call.name, "arguments": call.arguments_json}} for call in message.tool_calls]
        return wire
