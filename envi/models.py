from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .cancellation import CancellationToken


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments_json: str
    id: str = ""


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] | None = None


@dataclass(frozen=True)
class ModelReply:
    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None


class ModelClient(Protocol):
    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[dict[str, Any]],
                 token: CancellationToken) -> ModelReply: ...


class Router(Protocol):
    """A future classifier adapter can implement the same interface; None means abstain."""
    def try_route(self, text: str) -> ToolCall | None: ...


class Tools(Protocol):
    @property
    def schemas(self) -> Sequence[dict[str, Any]]: ...
    def validate(self, call: ToolCall) -> str | None: ...
    def requires_confirmation(self, call: ToolCall) -> bool: ...
    def describe(self, call: ToolCall) -> str: ...
    def execute(self, call: ToolCall, token: CancellationToken) -> str: ...


class EventSink(Protocol):
    def record(self, event_type: str, data: dict[str, Any] | None = None) -> None: ...


Confirmation = Callable[[ToolCall], bool]
