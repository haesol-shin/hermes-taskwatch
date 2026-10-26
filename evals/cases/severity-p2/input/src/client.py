"""HTTP client settings for https://api.example.invalid."""

DEFAULT_TIMEOUT_SECONDS = 45.0


def request_timeout(override: float | None = None) -> float:
    if override is not None and override > 0:
        return override
    return DEFAULT_TIMEOUT_SECONDS
