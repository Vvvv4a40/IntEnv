"""Local routing and allowlist tests: no model, network, or process launch."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from envi.cancellation import CancellationToken
from envi.configuration import AppSettings
from envi.errors import AssistantError, CancelledError
from envi.models import ToolCall
from envi.routing import RuleRouter
from envi.tools import ToolRegistry


class RouterAndToolTests(unittest.TestCase):
    def test_router_only_complete_unambiguous_phrases(self):
        router = RuleRouter()
        self.assertEqual("get_time", router.try_route("Который час?").name)
        self.assertEqual("open_app", router.try_route("Открой блокнот").name)
        self.assertEqual("open_folder", router.try_route("Открой папку проекта").name)
        for phrase in (
            "Который час и открой блокнот",
            "Не открывай блокнот",
            "Если будет время, открой калькулятор",
            "Открой блокнот; удали файлы",
            "Сколько времени до встречи?",
            "Открой папку проекта и запусти shell",
            "открой калькулятор\nзатем сделай перевод",
            "Запусти PowerShell",
        ):
            with self.subTest(phrase=phrase):
                self.assertIsNone(router.try_route(phrase))

    def test_reject_commands_paths_extra_keys_and_malformed_arguments(self):
        registry = ToolRegistry(AppSettings())
        calls = (
            ToolCall("run_powershell", '{"command":"shutdown"}'),
            ToolCall("open_app", '{"app":"C:\\\\Windows\\\\System32\\\\cmd.exe"}'),
            ToolCall("open_app", '{"app":"notepad","arguments":"/c del *"}'),
            ToolCall("open_app", '{"app":42}'),
            ToolCall("open_app", '{"app":"notepad","app":"calc"}'),
            ToolCall("open_app", '{"app":"notepad"}'),
            ToolCall("open_folder", '{"folder":"C:\\\\Users"}'),
            ToolCall("get_time", '{"command":"shutdown"}'),
            ToolCall("get_time", "[]"),
            ToolCall("get_time", "{"),
            ToolCall("get_time", ""),
            ToolCall("open_app", '{"app":NaN}'),
            ToolCall("get_time", '{"extra":Infinity}'),
        )
        for call in calls:
            with self.subTest(call=call):
                self.assertIsNotNone(registry.validate(call))
        self.assertIsNone(registry.validate(ToolCall("get_time", "{}")))

    def test_schemas_expose_only_aliases_not_paths(self):
        settings = AppSettings()
        settings.apps["safe_app"] = str(Path(tempfile.gettempdir()) / "envi-safe-app.exe")
        settings.folders["project"] = "."
        registry = ToolRegistry(settings)
        schema_json = json.dumps(registry.schemas)
        for expected in ("get_time", "open_app", "safe_app", "open_folder", "project", "additionalProperties"):
            self.assertIn(expected, schema_json)
        self.assertNotIn("run_powershell", schema_json)
        self.assertNotIn(settings.apps["safe_app"], schema_json)

    def test_relative_folder_cannot_escape_base_directory(self):
        with tempfile.TemporaryDirectory(prefix="envi-tests-") as directory:
            settings = AppSettings()
            settings.base_directory = Path(directory)
            settings.folders.update({"project": ".", "escape": ".."})
            registry = ToolRegistry(settings)
            self.assertIsNone(registry.validate(ToolCall("open_folder", '{"folder":"project"}')))
            self.assertIsNotNone(registry.validate(ToolCall("open_folder", '{"folder":"escape"}')))
            self.assertIsNotNone(registry.validate(ToolCall("open_folder", '{"folder":"arbitrary"}')))

    def test_open_actions_require_confirmation_but_time_does_not(self):
        registry = ToolRegistry(AppSettings())
        self.assertFalse(registry.requires_confirmation(ToolCall("get_time", "{}")))
        self.assertTrue(registry.requires_confirmation(ToolCall("open_app", '{"app":"x"}')))
        self.assertTrue(registry.requires_confirmation(ToolCall("open_folder", '{"folder":"x"}')))

    def test_allowlist_is_copied_and_aliases_are_case_insensitive(self):
        with tempfile.TemporaryDirectory(prefix="envi-tests-") as directory:
            executable = Path(directory) / "fake.exe"
            executable.write_bytes(b"")
            settings = AppSettings(apps={"SafeApp": str(executable)})
            registry = ToolRegistry(settings)
            settings.apps["SafeApp"] = str(Path(directory) / "cmd.exe")
            call = ToolCall("open_app", '{"app":"SAFEAPP"}')
            self.assertIsNone(registry.validate(call))
            self.assertIn(str(executable.resolve()), registry.describe(call))

    def test_shell_executables_and_path_arguments_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="envi-tests-") as directory:
            for name in ("cmd.exe", "powershell.exe", "pwsh.exe", "wsl.exe", "bash.exe", "wscript.exe"):
                executable = Path(directory) / name
                executable.write_bytes(b"")
                registry = ToolRegistry(AppSettings(apps={"app": str(executable)}))
                self.assertIsNotNone(registry.validate(ToolCall("open_app", '{"app":"app"}')))
            registry = ToolRegistry(AppSettings(apps={"app": str(Path(directory) / "safe.exe") + " --arg"}))
            self.assertIsNotNone(registry.validate(ToolCall("open_app", '{"app":"app"}')))

    @unittest.skipUnless(os.name == "nt", "Windows-only launch contract")
    def test_app_execution_uses_one_fixed_path_and_no_shell_or_user_arguments(self):
        with tempfile.TemporaryDirectory(prefix="envi-tests-") as directory:
            executable = Path(directory) / "fake program.exe"
            executable.write_bytes(b"")
            registry = ToolRegistry(AppSettings(apps={"app": str(executable)}))
            with patch.dict(os.environ, {"GROQ_API_KEY": "test-secret-never-inherit",
                                         "ENVI_TEST_CHILD_VALUE": "preserved"}), \
                    patch("envi.tools.subprocess.Popen") as launch:
                result = registry.execute(ToolCall("open_app", '{"app":"app"}'), CancellationToken())
                self.assertEqual(1, launch.call_count)
                self.assertEqual(([str(executable.resolve())],), launch.call_args.args)
                kwargs = launch.call_args.kwargs
                self.assertFalse(kwargs["shell"])
                self.assertTrue(kwargs["close_fds"])
                # Do not compare/dump the entire environment: it may contain unrelated secrets.
                child_env = kwargs["env"]
                self.assertFalse(any(name.casefold() == "groq_api_key" for name in child_env))
                self.assertEqual("preserved", child_env.get("ENVI_TEST_CHILD_VALUE"))
            self.assertIn("запрошен", result)

    def test_missing_target_is_revalidated_before_execution(self):
        with tempfile.TemporaryDirectory(prefix="envi-tests-") as directory:
            executable = Path(directory) / "fake.exe"
            executable.write_bytes(b"")
            registry = ToolRegistry(AppSettings(apps={"app": str(executable)}))
            call = ToolCall("open_app", '{"app":"app"}')
            self.assertIsNone(registry.validate(call))
            executable.unlink()
            with patch("envi.tools.subprocess.Popen") as launch:
                with self.assertRaises(AssistantError):
                    registry.execute(call, CancellationToken())
                launch.assert_not_called()

    def test_environment_target_change_is_rejected_before_launch(self):
        with tempfile.TemporaryDirectory(prefix="envi-tests-") as directory:
            first = Path(directory) / "first.exe"
            second = Path(directory) / "second.exe"
            first.write_bytes(b"")
            second.write_bytes(b"")
            with patch.dict(os.environ, {"ENVI_TEST_TARGET": str(first)}):
                registry = ToolRegistry(AppSettings(apps={"app": "%ENVI_TEST_TARGET%"}))
                call = ToolCall("open_app", '{"app":"app"}')
                self.assertIsNone(registry.validate(call))
                os.environ["ENVI_TEST_TARGET"] = str(second)
                self.assertIsNotNone(registry.validate(call))
                with patch("envi.tools.subprocess.Popen") as launch:
                    with self.assertRaises(AssistantError):
                        registry.execute(call, CancellationToken())
                    launch.assert_not_called()

    def test_relative_symlink_cannot_escape_base_directory(self):
        with tempfile.TemporaryDirectory(prefix="envi-tests-") as directory:
            base = Path(directory) / "base"
            outside = Path(directory) / "outside"
            base.mkdir()
            outside.mkdir()
            link = base / "link"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Directory symlinks unavailable without extra Windows privileges")
            registry = ToolRegistry(AppSettings(base_directory=base, folders={"escape": "link"}))
            self.assertIsNotNone(registry.validate(ToolCall("open_folder", '{"folder":"escape"}')))

    def test_cancelled_time_execution_raises_before_any_effect(self):
        token = CancellationToken()
        token.cancel()
        registry = ToolRegistry(AppSettings())
        with self.assertRaises(CancelledError):
            registry.execute(ToolCall("get_time", "{}"), token)


if __name__ == "__main__":
    unittest.main()
