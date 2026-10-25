"""Delay between retries."""

BASE_SECONDS = 2.0
CAP_SECONDS = 60.0


def retry_delay(attempt: int) -> float:
    delay = BASE_SECONDS
    for _ in range(attempt):
        if delay >= CAP_SECONDS:
            break
        delay *= 2
    return min(delay, CAP_SECONDS)
