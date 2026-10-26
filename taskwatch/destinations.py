"""A destination is a JSON subscription spec, not a chat id in code."""

import json

_REQUIRED = ("platform", "chat_id")
_OPTIONAL = ("thread_id", "chat_type", "profile", "delivery_mode")


class DestinationError(ValueError):
    """The destination cannot be subscribed."""


def parse_destination(text: str) -> dict[str, str]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DestinationError("destination is not a subscription spec") from exc
    if not isinstance(raw, dict):
        raise DestinationError("destination is not a subscription spec")
    unknown = set(raw) - set(_REQUIRED) - set(_OPTIONAL)
    if unknown:
        raise DestinationError(f"destination: unknown {sorted(unknown)[0]}")
    spec = {}
    for key in _REQUIRED:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise DestinationError(f"destination.{key} must be a non-empty string")
        spec[key] = value
    for key in _OPTIONAL:
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, str) or not value.strip():
            raise DestinationError(f"destination.{key} must be a non-empty string")
        spec[key] = value
    return spec
