"""Watch playback position and report a stall."""

STALL_AFTER_SECONDS = 120.0
ADVANCED = 0.05


def note_progress(position, previous, last_progress_at, now, paused):
    if paused:
        return previous, last_progress_at
    if position > previous + ADVANCED:
        previous = position
    last_progress_at = now
    return previous, last_progress_at


def is_stalled(last_progress_at, now, paused) -> bool:
    if paused:
        return False
    return now - last_progress_at > STALL_AFTER_SECONDS
