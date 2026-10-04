from collections.abc import Callable
from threading import Event, RLock

from .errors import CancelledError


class CancellationToken:
    def __init__(self) -> None:
        self._event = Event()
        self._lock = RLock()
        self._callbacks: dict[object, Callable[[], None]] = {}

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self.is_cancelled:
            raise CancelledError()

    def wait(self, seconds: float) -> bool:
        return self._event.wait(seconds)

    def cancel(self) -> None:
        with self._lock:
            self._event.set()
            callbacks, self._callbacks = self._callbacks, {}
        for callback in callbacks.values():
            self._invoke(callback)

    def register(self, callback: Callable[[], None]) -> Callable[[], None]:
        registration = object()
        with self._lock:
            cancelled = self.is_cancelled
            if not cancelled:
                self._callbacks[registration] = callback
        if cancelled:
            self._invoke(callback)

        def unregister() -> None:
            with self._lock:
                self._callbacks.pop(registration, None)

        return unregister

    @staticmethod
    def _invoke(callback: Callable[[], None]) -> None:
        try:
            callback()
        except Exception:
            pass
