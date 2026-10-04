import json
from typing import Any


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Повторяющееся поле JSON.")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Недопустимая числовая константа JSON.")


def strict_json_loads(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
