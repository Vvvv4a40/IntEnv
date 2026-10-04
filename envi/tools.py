"""Small, explicitly configured Windows actions; never execute model-supplied code.

Schemas expose aliases, not filesystem paths. A call is validated once before
confirmation and again immediately before execution. Configuration is copied so
editing AppSettings cannot silently change an already confirmed target.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .cancellation import CancellationToken
from .configuration import ALIAS_PATTERN, AppSettings, normalize_aliases
from .errors import AssistantError
from .json_utils import strict_json_loads
from .models import ToolCall


_BLOCKED_EXECUTABLES = frozenset({
    "cmd.exe", "powershell.exe", "pwsh.exe", "wsl.exe", "bash.exe", "sh.exe",
    "wscript.exe", "cscript.exe", "mshta.exe", "rundll32.exe", "regsvr32.exe",
})


class ToolRegistry:
    """Local allowlist boundary shared by rules, a future classifier and Groq."""

    def __init__(self, settings: AppSettings) -> None:
        self._base_directory = Path(settings.base_directory).resolve()
        self._apps = normalize_aliases(settings.apps)
        self._folders = normalize_aliases(settings.folders)
        self._resolved_targets: dict[tuple[str, str], Path] = {}
        self.schemas = [self._schema("get_time", "Получить текущее местное время.")]
        if self._apps:
            self.schemas.append(self._schema(
                "open_app", "Открыть разрешённое приложение Windows. Требуется подтверждение пользователя.",
                "app", sorted(self._apps),
            ))
        if self._folders:
            self.schemas.append(self._schema(
                "open_folder", "Открыть разрешённую папку в Проводнике Windows. Требуется подтверждение пользователя.",
                "folder", sorted(self._folders),
            ))

    def validate(self, call: ToolCall) -> str | None:
        """Return a safe Russian error, or None for a call safe to consider."""
        if not isinstance(call, ToolCall):
            return "Вызов инструмента отсутствует или имеет неверный формат."
        expected = {"get_time": None, "open_app": "app", "open_folder": "folder"}
        if not isinstance(call.name, str) or call.name not in expected:
            return "Неизвестный инструмент."
        if not isinstance(call.arguments_json, str) or not call.arguments_json.strip() or len(call.arguments_json) > 4096:
            return "Аргументы инструмента должны быть небольшим объектом JSON."
        try:
            arguments = strict_json_loads(call.arguments_json)
        except (ValueError, TypeError, RecursionError):
            return "Не удалось разобрать JSON аргументов инструмента."
        if not isinstance(arguments, dict):
            return "Аргументы инструмента должны быть объектом JSON."
        property_name = expected[call.name]
        if set(arguments) != ({property_name} if property_name else set()):
            return "У инструмента есть лишние или отсутствующие аргументы."
        if property_name is None:
            return None
        alias = arguments[property_name]
        if not isinstance(alias, str) or not ALIAS_PATTERN.fullmatch(alias):
            return f"Аргумент {property_name} должен быть короткой строкой из разрешённого списка."
        aliases = self._apps if call.name == "open_app" else self._folders
        configured_path = aliases.get(alias.casefold())
        if configured_path is None:
            return "Приложение отсутствует в разрешённом списке." if call.name == "open_app" else "Папка отсутствует в разрешённом списке."
        try:
            self._target(call.name, alias.casefold(), configured_path)
        except AssistantError as error:
            return str(error)
        return None

    @staticmethod
    def requires_confirmation(call: ToolCall) -> bool:
        return not isinstance(call, ToolCall) or call.name != "get_time"

    def describe(self, call: ToolCall) -> str:
        error = self.validate(call)
        if error:
            raise AssistantError(error)
        if call.name == "get_time":
            return "Показать текущее местное время."
        property_name = "app" if call.name == "open_app" else "folder"
        alias = self._read_alias(call, property_name)
        if call.name == "open_app":
            return f"Открыть приложение «{alias}»: {self._target(call.name, alias, self._apps[alias])}"
        return f"Открыть папку «{alias}»: {self._target(call.name, alias, self._folders[alias])}"

    def execute(self, call: ToolCall, token: CancellationToken) -> str:
        """Revalidate immediately before starting one fixed executable without a shell."""
        token.check()
        error = self.validate(call)
        if error:
            raise AssistantError(error)
        if call.name == "get_time":
            return "Сейчас " + datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S %z") + "."
        if os.name != "nt":
            raise AssistantError("Открытие приложений и папок доступно только в Windows.")
        try:
            if call.name == "open_app":
                alias = self._read_alias(call, "app")
                path = self._target(call.name, alias, self._apps[alias])
                command = [str(path)]
                result = f"Запуск приложения «{alias}» запрошен."
            else:
                alias = self._read_alias(call, "folder")
                folder = self._target(call.name, alias, self._folders[alias])
                windows_directory = os.environ.get("SystemRoot", os.environ.get("WINDIR", ""))
                explorer = Path(windows_directory) / "explorer.exe"
                if not windows_directory or not explorer.is_absolute() or not explorer.is_file():
                    raise AssistantError("Не найден Проводник Windows.")
                command = [str(explorer), str(folder)]
                result = f"Открытие папки «{alias}» запрошено."
            token.check()
            # A launched app needs its normal environment, not Envi's API key.
            child_env = os.environ.copy()
            for key in tuple(child_env):
                if key.casefold() == "groq_api_key":
                    child_env.pop(key)
            subprocess.Popen(command, shell=False, close_fds=True, env=child_env)
            # Successful process creation is not proof of an opened app window.
            return result
        except OSError as error:
            raise AssistantError("Windows не удалось запустить разрешённое действие. Проверь путь и права доступа.") from error

    @staticmethod
    def _schema(name: str, description: str, argument: str | None = None,
                aliases: list[str] | None = None) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": {} if argument is None else {argument: {"type": "string", "enum": aliases}},
                    "required": [] if argument is None else [argument],
                    "additionalProperties": False,
                },
            },
        }

    @staticmethod
    def _read_alias(call: ToolCall, property_name: str) -> str:
        return strict_json_loads(call.arguments_json)[property_name].casefold()

    def _target(self, name: str, alias: str, configured: str) -> Path:
        path = self._resolve_app(configured) if name == "open_app" else self._resolve_folder(configured)
        key = (name, alias)
        previous = self._resolved_targets.get(key)
        if previous is not None and previous != path:
            raise AssistantError("Разрешённая цель изменилась. Перезапусти Envi и проверь настройки перед подтверждением.")
        self._resolved_targets.setdefault(key, path)
        return path

    @staticmethod
    def _expanded_path(configured: str) -> Path:
        if not isinstance(configured, str) or not configured.strip() or any(ord(char) < 32 for char in configured):
            raise AssistantError("Путь в настройках некорректен.")
        # Support the existing %LOCALAPPDATA% configuration, even in offline
        # tests under a non-Windows Python. Never expand model-supplied text.
        expanded = re.sub(r"%([^%]+)%", lambda match: os.environ.get(match[1], match[0]), configured)
        if re.search(r"%[^%]+%", expanded):
            raise AssistantError("Переменная окружения в пути не определена.")
        return Path(expanded)

    @classmethod
    def _resolve_app(cls, configured: str) -> Path:
        try:
            path = cls._expanded_path(configured)
            if not path.is_absolute() or path.suffix.casefold() != ".exe":
                raise AssistantError("В настройках приложения требуется полный путь к файлу .exe, без аргументов.")
            if path.name.casefold() in _BLOCKED_EXECUTABLES:
                raise AssistantError("Запуск командной оболочки не разрешён.")
            path = path.resolve(strict=True)
            if not path.is_file() or path.suffix.casefold() != ".exe":
                raise AssistantError("Файл приложения из настроек не найден.")
            if path.name.casefold() in _BLOCKED_EXECUTABLES:
                raise AssistantError("Запуск командной оболочки не разрешён.")
            return path
        except (OSError, ValueError, RuntimeError) as error:
            raise AssistantError("Файл приложения из настроек не найден или путь некорректен.") from error

    def _resolve_folder(self, configured: str) -> Path:
        try:
            path = self._expanded_path(configured)
            relative = not path.is_absolute()
            # Windows drive-relative paths (C:foo) and root-relative paths
            # (\\foo) must not be interpreted relative to process state.
            if relative and (path.anchor or path.drive):
                raise AssistantError("Путь к папке должен быть полным или относительным от каталога проекта.")
            path = (self._base_directory / path if relative else path).resolve(strict=True)
            if relative and not path.is_relative_to(self._base_directory):
                raise AssistantError("Относительный путь к папке выходит за пределы каталога проекта.")
            if not path.is_dir():
                raise AssistantError("Папка из настроек не найдена.")
            return path
        except (OSError, ValueError, RuntimeError) as error:
            raise AssistantError("Папка из настроек не найдена или путь некорректен.") from error
