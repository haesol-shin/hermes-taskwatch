"""Start playback for a page on https://media.example.invalid/watch."""

START_WAIT_SECONDS = 60.0


class PlaybackError(Exception):
    pass


def start_playback(player, clock) -> None:
    deadline = clock.monotonic() + START_WAIT_SECONDS
    while clock.monotonic() < deadline:
        if player.started():
            return
        clock.sleep(0.5)
    raise PlaybackError("playback-not-started")
