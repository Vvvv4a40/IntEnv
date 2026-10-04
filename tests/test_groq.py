"""Groq wire-contract tests. Scripted transport never opens a network socket."""

import io
import json
import unittest
import wave
from unittest.mock import patch

from envi.assistant import AssistantService
from envi.cancellation import CancellationToken
from envi.configuration import AppSettings
from envi.errors import AssistantError, CancelledError
from envi.models import ChatMessage, ToolCall
from envi.providers.groq import GroqClient, HttpResponse, StandardHttpTransport
from envi.routing import RuleRouter
from envi.tools import ToolRegistry

from tests.helpers import FakeEvents, FakeTransport


def wav_bytes(seconds=1, rate=16000, channels=1, sample_width=2):
    with io.BytesIO() as output:
        with wave.open(output, "wb") as stream:
            stream.setnchannels(channels)
            stream.setsampwidth(sample_width)
            stream.setframerate(rate)
            stream.writeframes(bytes(seconds * rate * channels * sample_width))
        return output.getvalue()


def chat_response(content="Hello", calls=None, finish="stop"):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    return HttpResponse(200, json.dumps({"choices": [{"finish_reason": finish,
                                                     "message": message}]}).encode("utf-8"), {})


class GroqTests(unittest.TestCase):
    def client(self, *responses, settings=None, key="unit-test-key"):
        self.settings = settings or AppSettings()
        self.transport = FakeTransport(*responses)
        return GroqClient(self.settings, key, self.transport)

    def test_stt_sends_configured_russian_multipart_request(self):
        client = self.client(HttpResponse(200, "  привет, мир  ".encode("utf-8"), {}),
                             settings=AppSettings(stt_model="test-whisper"))
        transcript = client.transcribe(wav_bytes(), CancellationToken())
        self.assertEqual("привет, мир", transcript)
        self.assertEqual(1, client.session_api_requests)
        url, headers, body, timeout = self.transport.requests[0]
        self.assertEqual("https://api.groq.com/openai/v1/audio/transcriptions", url)
        self.assertEqual("Bearer unit-test-key", headers["Authorization"])
        self.assertIn("multipart/form-data", headers["Content-Type"])
        for value in (b'name="file"', b'audio.wav', b'test-whisper', b'name="language"',
                      b'\r\nru\r\n', b'name="response_format"', b'\r\ntext\r\n'):
            self.assertIn(value, body)
        self.assertEqual(30, timeout)

    def test_chat_sends_roles_schemas_limits_and_parses_tool_calls(self):
        wire_calls = [{"id": "call-9", "type": "function",
                       "function": {"name": "get_time", "arguments": "{}"}}]
        client = self.client(chat_response(None, wire_calls, "tool_calls"),
                             settings=AppSettings(chat_model="test-chat", max_output_tokens=321))
        messages = (ChatMessage("system", "system prompt"), ChatMessage("user", "tell me time"),
                    ChatMessage("assistant", tool_calls=(ToolCall("get_time", "{}", "old-1"),)),
                    ChatMessage("tool", "Сейчас 12:00", "old-1"))
        schemas = ({"type": "function", "function": {"name": "get_time"}},)
        reply = client.complete(messages, schemas, CancellationToken())
        self.assertEqual("tool_calls", reply.finish_reason)
        self.assertEqual((ToolCall("get_time", "{}", "call-9"),), tuple(reply.tool_calls))
        url, headers, body, _ = self.transport.requests[0]
        self.assertEqual("https://api.groq.com/openai/v1/chat/completions", url)
        self.assertEqual("Bearer unit-test-key", headers["Authorization"])
        data = json.loads(body)
        self.assertEqual("test-chat", data["model"])
        self.assertEqual(321, data["max_completion_tokens"])
        self.assertEqual("auto", data["tool_choice"])
        self.assertEqual(list(schemas), data["tools"])
        self.assertEqual(["system", "user", "assistant", "tool"], [m["role"] for m in data["messages"]])
        self.assertEqual("old-1", data["messages"][2]["tool_calls"][0]["id"])
        self.assertEqual("old-1", data["messages"][3]["tool_call_id"])

    def test_429_surface_without_retry_or_body_secrets(self):
        client = self.client(HttpResponse(429, b'{"error":{"message":"SECRET_PROVIDER_BODY"}}', {}))
        with self.assertRaises(AssistantError) as caught:
            client.complete((ChatMessage("user", "hello"),), (), CancellationToken())
        self.assertIn("429", str(caught.exception))
        self.assertNotIn("SECRET_PROVIDER_BODY", str(caught.exception))
        self.assertEqual(1, len(self.transport.requests))
        self.assertEqual(1, client.session_api_requests)

    def test_stt_and_chat_share_session_request_quota(self):
        client = self.client(HttpResponse(200, "привет".encode("utf-8"), {}),
                             settings=AppSettings(max_session_api_requests=1))
        self.assertEqual("привет", client.transcribe(wav_bytes(), CancellationToken()))
        with self.assertRaises(AssistantError) as caught:
            client.complete((ChatMessage("user", "hello"),), (), CancellationToken())
        self.assertIn("лимит", str(caught.exception))
        self.assertEqual(1, len(self.transport.requests))
        self.assertEqual(1, client.session_api_requests)

    def test_missing_key_does_not_block_local_time(self):
        client = self.client(key=None)
        service = AssistantService(client, ToolRegistry(self.settings), RuleRouter(), self.settings, FakeEvents())
        answer = service.process("Который час?", lambda call: self.fail("Unexpected approval"), CancellationToken())
        self.assertIn("Сейчас", answer)
        self.assertEqual([], self.transport.requests)
        self.assertEqual(0, client.session_api_requests)

    def test_missing_key_cloud_fails_before_transport_and_quota(self):
        for key in (None, "", "   "):
            with self.subTest(key=key):
                client = self.client(key=key)
                with self.assertRaises(AssistantError):
                    client.complete((ChatMessage("user", "hello"),), (), CancellationToken())
                self.assertEqual([], self.transport.requests)
                self.assertEqual(0, client.session_api_requests)

    def test_invalid_audio_fails_before_transport_and_quota(self):
        for audio in (b"", bytes(44), b"not WAV", wav_bytes(seconds=31)):
            with self.subTest(size=len(audio)):
                client = self.client()
                with self.assertRaises(AssistantError):
                    client.transcribe(audio, CancellationToken())
                self.assertEqual([], self.transport.requests)
                self.assertEqual(0, client.session_api_requests)

    def test_empty_transcription_has_friendly_error(self):
        client = self.client(HttpResponse(200, b"   ", {}))
        with self.assertRaises(AssistantError):
            client.transcribe(wav_bytes(), CancellationToken())
        self.assertEqual(1, len(self.transport.requests))

    def test_cancelled_request_never_touches_transport(self):
        token = CancellationToken()
        token.cancel()
        client = self.client()
        with self.assertRaises(CancelledError):
            client.complete((ChatMessage("user", "hello"),), (), token)
        self.assertEqual([], self.transport.requests)
        self.assertEqual(0, client.session_api_requests)

    def test_bad_json_or_malformed_completion_has_friendly_error(self):
        bodies = (b"{", b"[]", b'{"choices":[]}',
                  b'{"choices":[{"message":{"content":42}}]}',
                  b'{"choices":[{"message":{"tool_calls":[{"id":"x","function":{"name":"get_time","arguments":{}}}]}}]}')
        for body in bodies:
            with self.subTest(body=body):
                client = self.client(HttpResponse(200, body, {}))
                with self.assertRaises(AssistantError):
                    client.complete((ChatMessage("user", "hello"),), (), CancellationToken())
                self.assertEqual(1, len(self.transport.requests))

    def test_tool_calls_must_be_an_array_even_when_value_is_empty(self):
        for calls in ({}, "", 0, False, {"call": "invalid"}):
            with self.subTest(calls=calls):
                client = self.client(chat_response("Hello", calls))
                with self.assertRaises(AssistantError) as caught:
                    client.complete((ChatMessage("user", "hello"),), (), CancellationToken())
                self.assertIn("формата", str(caught.exception))
                self.assertEqual(1, len(self.transport.requests))

    def test_empty_or_null_tool_call_array_is_a_valid_text_reply(self):
        for calls in (None, []):
            with self.subTest(calls=calls):
                body = {"choices": [{"message": {"content": "Hello", "tool_calls": calls}}]}
                client = self.client(HttpResponse(200, json.dumps(body).encode("utf-8"), {}))
                reply = client.complete((ChatMessage("user", "hello"),), (), CancellationToken())
                self.assertEqual("Hello", reply.content)
                self.assertEqual((), reply.tool_calls)

    def test_duplicate_json_fields_and_nonstandard_constants_are_rejected(self):
        bodies = (b'{"choices":[],"choices":[{"message":{"content":"Hello"}}]}',
                  b'{"choices":[{"message":{"content":"Hello","tool_calls":NaN}}]}')
        for body in bodies:
            with self.subTest(body=body):
                client = self.client(HttpResponse(200, body, {}))
                with self.assertRaises(AssistantError):
                    client.complete((ChatMessage("user", "hello"),), (), CancellationToken())

    def test_cancelled_transcription_stops_before_audio_validation_and_quota(self):
        token = CancellationToken()
        token.cancel()
        client = self.client()
        with self.assertRaises(CancelledError):
            client.transcribe(b"invalid WAV", token)
        self.assertEqual([], self.transport.requests)
        self.assertEqual(0, client.session_api_requests)

    def test_http_auth_and_server_errors_do_not_retry(self):
        for status in (401, 403, 500, 503):
            with self.subTest(status=status):
                client = self.client(HttpResponse(status, b"secret provider detail", {}))
                with self.assertRaises(AssistantError):
                    client.complete((ChatMessage("user", "hello"),), (), CancellationToken())
                self.assertEqual(1, len(self.transport.requests))

    def test_header_control_characters_and_nonascii_key_rejected_before_quota(self):
        for key in ("key\r\nInjected: evil", "key with space", "ключ", "key\x00value", "key\tvalue"):
            with self.subTest(key=repr(key)):
                client = self.client(key=key)
                with self.assertRaises(AssistantError):
                    client.complete((ChatMessage("user", "hello"),), (), CancellationToken())
                self.assertEqual([], self.transport.requests)
                self.assertEqual(0, client.session_api_requests)

    def test_oversized_response_rejected_without_retry(self):
        client = self.client(HttpResponse(200, b"x" * 1_048_577, {}))
        with self.assertRaises(AssistantError):
            client.complete((ChatMessage("user", "hello"),), (), CancellationToken())
        self.assertEqual(1, len(self.transport.requests))

    def test_only_fixed_groq_endpoints_permitted_by_standard_transport(self):
        transport = StandardHttpTransport()
        for url in ("https://example.com/openai/v1/chat/completions", "http://api.groq.com/openai/v1/chat/completions",
                    "https://api.groq.com.evil.test/openai/v1/chat/completions",
                    "https://api.groq.com/openai/v1/chat/completions?redirect=evil",
                    "https://api.groq.com/openai/v1/chat/completions#fragment",
                    "https://api.groq.com/openai/v1/unknown"):
            with self.subTest(url=url), patch("envi.providers.groq.http.client.HTTPSConnection") as connection:
                with self.assertRaises(AssistantError):
                    transport.send(url, {"Authorization": "Bearer fake"}, b"{}", 30, CancellationToken())
                connection.assert_not_called()

    def test_no_tools_omits_tool_fields_and_default_model_restricts_reasoning(self):
        client = self.client(chat_response())
        client.complete((ChatMessage("user", "hello"),), (), CancellationToken())
        body = json.loads(self.transport.requests[0][2])
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)
        self.assertEqual("low", body["reasoning_effort"])

    def test_wrong_pcm_format_or_truncated_wav_never_reaches_network(self):
        audio_cases = (wav_bytes(rate=8000), wav_bytes(channels=2), wav_bytes(sample_width=1), wav_bytes()[:-8])
        for audio in audio_cases:
            with self.subTest(size=len(audio)):
                client = self.client()
                with self.assertRaises(AssistantError):
                    client.transcribe(audio, CancellationToken())
                self.assertEqual([], self.transport.requests)
                self.assertEqual(0, client.session_api_requests)

    def test_oversized_or_invalid_utf8_transcription_is_rejected(self):
        for body in (b"a" * 4001, b"\xff\xfe"):
            with self.subTest(size=len(body)):
                client = self.client(HttpResponse(200, body, {}))
                with self.assertRaises(AssistantError):
                    client.transcribe(wav_bytes(), CancellationToken())
                self.assertEqual(1, len(self.transport.requests))


if __name__ == "__main__":
    unittest.main()
