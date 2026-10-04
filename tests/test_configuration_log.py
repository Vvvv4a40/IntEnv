"""Configuration, cancellation and metadata-only journal checks."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from envi.cancellation import CancellationToken
from envi.configuration import AppSettings
from envi.errors import AssistantError
from envi.event_log import JsonEventLog
from envi.assistant import AssistantService
from envi.providers.groq import GroqClient, HttpResponse
from envi.routing import RuleRouter
from envi.tools import ToolRegistry
from tests.helpers import FakeTransport


class ConfigurationTests(unittest.TestCase):
    def test_load_accepts_legacy_keys_and_normalizes_aliases(self):
        with tempfile.TemporaryDirectory(prefix="envi-tests-") as directory:
            path = Path(directory) / "envi.settings.json"
            path.write_text(json.dumps({"chatModel": "test-chat", "sttModel": "test-stt", "language": "ru",
                                        "apps": {"MyApp": "C:\\fake\\app.exe"},
                                        "folders": {"Project": "."}}), encoding="utf-8")
            settings = AppSettings.load(path)
            self.assertEqual("test-chat", settings.chat_model)
            self.assertEqual("test-stt", settings.stt_model)
            self.assertEqual(Path(directory).resolve(), settings.base_directory)
            self.assertIn("myapp", settings.apps)
            self.assertIn("project", settings.folders)

    def test_snake_case_settings_and_utf8_bom_are_supported(self):
        with tempfile.TemporaryDirectory(prefix="envi-tests-") as directory:
            path = Path(directory) / "envi.settings.json"
            path.write_text('{"chat_model":"test-chat","max_tool_rounds":2}', encoding="utf-8-sig")
            settings = AppSettings.load(path)
            self.assertEqual("test-chat", settings.chat_model)
            self.assertEqual(2, settings.max_tool_rounds)

    def test_invalid_limits_types_models_and_aliases_are_rejected(self):
        changes = (
            {"timeout_seconds": 4}, {"timeout_seconds": 121}, {"timeout_seconds": True},
            {"max_tool_rounds": 0}, {"max_tool_rounds": 6}, {"max_output_tokens": 127},
            {"max_output_tokens": 4097}, {"history_messages": 41}, {"history_messages": -1},
            {"max_session_api_requests": 0}, {"max_session_api_requests": 1001},
            {"recording_max_seconds": 0}, {"recording_max_seconds": 31},
            {"language": ""}, {"language": "too-long-language"}, {"chat_model": "   "},
            {"apps": {"unsafe name": "app.exe"}}, {"apps": {"same": "a.exe", "SAME": "b.exe"}},
            {"folders": {"folder": ""}}, {"folders": []}, {"stt_model": None},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(AssistantError):
                AppSettings(**change).validate()

    def test_unknown_duplicate_alias_keys_and_bad_json_rejected(self):
        inputs = (
            '{"SurprisePermission":"all"}',
            '{"chat_model":"one","ChatModel":"two"}',
            '{"timeout_seconds":30,"timeout_seconds":31}',
            '{"apps":{"app":"a.exe","app":"b.exe"}}',
            '{"timeout_seconds":NaN}',
            '[]', '{',
        )
        with tempfile.TemporaryDirectory(prefix="envi-tests-") as directory:
            path = Path(directory) / "envi.settings.json"
            for content in inputs:
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(AssistantError):
                        AppSettings.load(path)

    def test_missing_file_has_friendly_error(self):
        with tempfile.TemporaryDirectory(prefix="envi-tests-") as directory:
            with self.assertRaises(AssistantError):
                AppSettings.load(Path(directory) / "missing.json")

    def test_settings_and_registry_apply_the_same_alias_validation(self):
        invalid_aliases = (
            None,
            [],
            {"unsafe name": "app.exe"},
            {"same": "a.exe", "SAME": "b.exe"},
            {"app": None},
            {42: "app.exe"},
        )
        for field in ("apps", "folders"):
            for aliases in invalid_aliases:
                with self.subTest(field=field, aliases=aliases):
                    settings = AppSettings(**{field: aliases})
                    with self.assertRaises(AssistantError):
                        settings.validate()
                    with self.assertRaises(AssistantError):
                        ToolRegistry(settings)


class EventLogTests(unittest.TestCase):
    def test_log_drops_audio_text_and_key_fields(self):
        with tempfile.TemporaryDirectory(prefix="envi-tests-") as directory:
            path = Path(directory) / "logs" / "events.jsonl"
            log = JsonEventLog(path)
            log.record("request.route", {"route": "model", "length": 50,
                                         "prompt": "SECRET_PROMPT", "reply": "SECRET_REPLY",
                                         "api_key": "SECRET_KEY", "audio": "SECRET_AUDIO",
                                         "nested": {"password": "SECRET_PASSWORD"}})
            text = path.read_text(encoding="utf-8")
            entry = json.loads(text)
            self.assertEqual({"route": "model", "length": 50}, entry["data"])
            self.assertEqual("request.route", entry["eventType"])
            timestamp = datetime.fromisoformat(entry["timestamp"])
            self.assertIsNotNone(timestamp.tzinfo)
            self.assertEqual(timezone.utc.utcoffset(timestamp), timestamp.utcoffset())
            self.assertIsNone(log.last_error)
            self.assertNotIn("SECRET", text)

    def test_write_failure_is_nonfatal(self):
        with tempfile.TemporaryDirectory(prefix="envi-tests-") as directory:
            blocker = Path(directory) / "blocker"
            blocker.write_text("existing file", encoding="utf-8")
            log = JsonEventLog(blocker / "events.jsonl")
            log.record("request.route", {"length": 4})
            self.assertIsNotNone(log.last_error)

    def test_nonfinite_metadata_is_excluded_from_standard_json(self):
        with tempfile.TemporaryDirectory(prefix="envi-tests-") as directory:
            path = Path(directory) / "events.jsonl"
            log = JsonEventLog(path)
            log.record("model.completed", {"length": float("nan"), "toolCount": float("inf"),
                                           "round": float("-inf"), "name": "get_time"})
            self.assertEqual({"name": "get_time"}, json.loads(path.read_text(encoding="utf-8"))["data"])
            self.assertIsNone(log.last_error)

    def test_encoding_failure_is_nonfatal_and_the_next_event_recovers(self):
        with tempfile.TemporaryDirectory(prefix="envi-tests-") as directory:
            path = Path(directory) / "events.jsonl"
            log = JsonEventLog(path)
            log.record("request.route", {"route": "\ud800"})
            self.assertIsNotNone(log.last_error)
            log.record("request.route", {"route": "local", "length": 4})
            self.assertIsNone(log.last_error)
            self.assertEqual({"route": "local", "length": 4},
                             json.loads(path.read_text(encoding="utf-8"))["data"])

    def test_full_request_pipeline_does_not_log_key_prompt_or_answer(self):
        with tempfile.TemporaryDirectory(prefix="envi-tests-") as directory:
            path = Path(directory) / "events.jsonl"
            key, prompt, reply = "SECRET_API_KEY", "SECRET_PROMPT", "SECRET_ANSWER"
            body = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": reply}}]}).encode("utf-8")
            transport = FakeTransport(HttpResponse(200, body, {}))
            settings = AppSettings()
            log = JsonEventLog(path)
            model = GroqClient(settings, key, transport)
            assistant = AssistantService(model, ToolRegistry(settings), RuleRouter(), settings, log)
            self.assertEqual(reply, assistant.process(prompt, lambda call: False, CancellationToken()))
            log_text = path.read_text(encoding="utf-8")
            self.assertIn("request.route", log_text)
            self.assertIn("model.completed", log_text)
            for secret in (key, prompt, reply):
                self.assertNotIn(secret, log_text)


if __name__ == "__main__":
    unittest.main()
