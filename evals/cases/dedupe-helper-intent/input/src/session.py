"""Session lock errors. No network host."""


class SessionError(Exception):
    def __init__(self, code: str, message: str, hint: str) -> None:
        self.code = code
        self.hint = hint
        super().__init__(message)


def session_busy() -> SessionError:
    return SessionError(
        "session-busy",
        "Another operation holds the session lock.",
        "Wait for the other operation to finish, then retry.",
    )


def acquire(lock) -> None:
    if not lock.try_acquire():
        raise session_busy()


def renew(lock) -> None:
    if not lock.held():
        raise session_busy()
