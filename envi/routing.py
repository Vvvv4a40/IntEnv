"""Conservative local routing for a handful of complete, unambiguous phrases.

Unknown, compound, conditional, or negated requests go to the next router/model.
The router only proposes a call; the tool registry validates it independently.
"""

from __future__ import annotations

import re

from .models import ToolCall


_PHRASES: dict[str, tuple[str, str]] = {
    "сколько времени": ("get_time", "{}"),
    "сколько сейчас времени": ("get_time", "{}"),
    "который час": ("get_time", "{}"),
    "скажи время": ("get_time", "{}"),
    "покажи время": ("get_time", "{}"),
    "what time is it": ("get_time", "{}"),
    "tell me the time": ("get_time", "{}"),
    "открой папку проекта": ("open_folder", '{"folder":"project"}'),
    "покажи папку проекта": ("open_folder", '{"folder":"project"}'),
    "open project folder": ("open_folder", '{"folder":"project"}'),
    "open the project folder": ("open_folder", '{"folder":"project"}'),
    "show project folder": ("open_folder", '{"folder":"project"}'),
    "show the project folder": ("open_folder", '{"folder":"project"}'),
    "открой блокнот": ("open_app", '{"app":"notepad"}'),
    "запусти блокнот": ("open_app", '{"app":"notepad"}'),
    "открой notepad": ("open_app", '{"app":"notepad"}'),
    "запусти notepad": ("open_app", '{"app":"notepad"}'),
    "open notepad": ("open_app", '{"app":"notepad"}'),
    "launch notepad": ("open_app", '{"app":"notepad"}'),
    "открой калькулятор": ("open_app", '{"app":"calculator"}'),
    "запусти калькулятор": ("open_app", '{"app":"calculator"}'),
    "открой calculator": ("open_app", '{"app":"calculator"}'),
    "запусти calculator": ("open_app", '{"app":"calculator"}'),
    "открой calc": ("open_app", '{"app":"calculator"}'),
    "запусти calc": ("open_app", '{"app":"calculator"}'),
    "open calculator": ("open_app", '{"app":"calculator"}'),
    "launch calculator": ("open_app", '{"app":"calculator"}'),
    "open calc": ("open_app", '{"app":"calculator"}'),
    "launch calc": ("open_app", '{"app":"calculator"}'),
}

for _name in ("vs code", "vscode", "visual studio code"):
    for _verb in ("открой", "запусти", "open", "launch"):
        _PHRASES[f"{_verb} {_name}"] = ("open_app", '{"app":"vscode"}')


class RuleRouter:
    """Match complete phrases only; never infer intent from a substring."""

    def try_route(self, text: str) -> ToolCall | None:
        if not isinstance(text, str) or not text.strip() or len(text) > 256:
            return None

        normalized = re.sub(r"\s+", " ", text.strip().lower().replace("ё", "е"))
        if normalized.endswith(("?", ".", "!")):
            normalized = normalized[:-1]

        route = _PHRASES.get(normalized)
        return ToolCall(*route) if route is not None else None
