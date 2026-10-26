"""Shared playback messages for https://media.example.invalid/watch."""

NOT_STARTED_MESSAGE = "Playback did not start before the deadline."


class PlaybackError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)


def require_started(player) -> None:
    if not player.started():
        raise PlaybackError(NOT_STARTED_MESSAGE)


def require_picture(player) -> None:
    if not player.has_picture():
        raise PlaybackError(NOT_STARTED_MESSAGE)
