import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .errors import AssistantError
from .json_utils import strict_json_loads


@dataclass
class AppSettings:
    chat_model: str = "openai/gpt-oss-20b"
    stt_model: str = "whisper-large-v3-turbo"
    language: str = "ru"
    timeout_seconds: int = 30
    max_tool_rounds: int = 3
    max_output_tokens: int = 600
    history_messages: int = 12
    max_session_api_requests: int = 100
    recording_max_seconds: int = 30
    apps: dict[str, str] = field(default_factory=dict)
    folders: dict[str, str] = field(default_factory=dict)
    base_directory: Path = field(default_factory=Path.cwd)

    @classmethod
    def load(cls, path: str | Path) -> "AppSettings":
        try:
            full_path = Path(path).resolve()
            if full_path.stat().st_size > 1_048_576:
                raise AssistantError("Файл настроек слишком большой.")
            raw = strict_json_loads(full_path.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                raise AssistantError("Настройки должны быть объектом JSON.")
            # Accept the old CamelCase keys too, but reject aliases supplied twice.
            names = {f.name.replace("_", "").casefold(): f.name for f in fields(cls) if f.name != "base_directory"}
            kwargs: dict[str, Any] = {}
            for key, value in raw.items():
                name = names.get(key.replace("_", "").casefold())
                if name is None or name in kwargs:
                    raise AssistantError("Неизвестное или повторяющееся поле настроек: " + key)
                kwargs[name] = value
            settings = cls(**kwargs, base_directory=full_path.parent)
            settings.validate()
            settings.apps = {k.casefold(): v for k, v in settings.apps.items()}
            settings.folders = {k.casefold(): v for k, v in settings.folders.items()}
            return settings
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as error:
            raise AssistantError("Не удалось прочитать настройки. Проверь путь и JSON в envi.settings.json.") from error

    def validate(self) -> None:
        for name in ("chat_model", "stt_model", "language"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise AssistantError("В настройках нужны непустые имена моделей и язык.")
        if len(self.language) > 8:
            raise AssistantError("Некорректный код языка.")
        limits = {
            "timeout_seconds": (5, 120), "max_tool_rounds": (1, 5),
            "max_output_tokens": (128, 4096), "history_messages": (0, 40),
            "max_session_api_requests": (1, 1000), "recording_max_seconds": (1, 30),
        }
        for name, (minimum, maximum) in limits.items():
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise AssistantError(f"Некорректный лимит {name}: допустимо {minimum}–{maximum}.")
        for aliases in (self.apps, self.folders):
            if not isinstance(aliases, dict):
                raise AssistantError("Разрешённые приложения и папки должны быть объектами JSON.")
            seen: set[str] = set()
            for key, value in aliases.items():
                if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", key) or \
                        not isinstance(value, str) or not value.strip() or key.casefold() in seen:
                    raise AssistantError("В списках разрешений нужны уникальные короткие имена и непустые пути.")
                seen.add(key.casefold())
