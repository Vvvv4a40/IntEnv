from collections.abc import Callable
from threading import Event, RLock

from .errors import CancelledError


class CancellationToken:
    """Cooperative cancellation shared by UI, HTTP transport and core."""

    def __init__(self) -> None:
        self._event = Event()
        self._lock = RLock()
        self._callbacks: list[Callable[[], None]] = []

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
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            try:
                callback()
            except Exception:
                # A failed socket close cannot revoke cancellation or prevent other cleanups.
                pass

    def register(self, callback: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            cancelled = self.is_cancelled
            if not cancelled:
                self._callbacks.append(callback)
        if cancelled:
            callback()

        def unregister() -> None:
            with self._lock:
                if callback in self._callbacks:
                    self._callbacks.remove(callback)

        return unregister
