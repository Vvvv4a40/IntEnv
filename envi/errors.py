class AssistantError(Exception):
    """A safe, user-facing error; never include keys or raw provider responses."""


class CancelledError(AssistantError):
    def __init__(self, message: str = "Запрос отменён.") -> None:
        super().__init__(message)
