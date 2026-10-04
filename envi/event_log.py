import json
import math
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any


class JsonEventLog:
    """Metadata only; full prompts, replies, audio and keys must not enter the log."""
    _allowed_fields = {"route", "length", "name", "toolCount", "round"}

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = Lock()
        self.last_error: str | None = None

    def record(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        metadata = {key: value for key, value in (data or {}).items()
                    if key in self._allowed_fields and type(value) in (str, int, bool, float)
                    and (type(value) is not float or math.isfinite(value))}
        line = json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
                           "eventType": event_type, "data": metadata}, ensure_ascii=False, allow_nan=False)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(line + "\n")
                self.last_error = None
            except (OSError, UnicodeError):
                self.last_error = "Не удалось записать журнал событий."
