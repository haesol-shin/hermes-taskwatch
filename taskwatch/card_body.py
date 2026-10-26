"""Card body carries the approval key and a JSON block of taskwatch fields.

Hermes has no columns for those fields. The block is the card's pointer, not a
second copy of the intent or the spec.
"""

import json
import re

_BLOCK = re.compile(r"<!-- taskwatch\n(.*?)\n-->", re.DOTALL)
_KEYS = frozenset(
    {
        "gate",
        "kind",
        "approval_key",
        "intent_path",
        "spec_path",
        "applied_fingerprint",
        "time_limit_seconds",
        "ignored_event_ids",
        "snapshot_path",
        "warning_for",
    }
)


def dumps(fields: dict[str, object], prose: str = "") -> str:
    payload = {key: fields[key] for key in _KEYS if key in fields and fields[key] is not None}
    block = "<!-- taskwatch\n" + json.dumps(payload, sort_keys=True) + "\n-->"
    text = prose.strip()
    if payload.get("approval_key"):
        line = f"approval_key: {payload['approval_key']}"
        text = f"{text}\n{line}".strip() if text else line
    return f"{text}\n{block}\n" if text else f"{block}\n"


def loads(body: str | None) -> dict[str, object]:
    if not body:
        return {}
    match = _BLOCK.search(body)
    if match is None:
        return {}
    try:
        raw = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {key: raw[key] for key in _KEYS if key in raw}


def replace_fields(body: str | None, updates: dict[str, object]) -> str:
    """Return the body with taskwatch fields replaced. Prose stays."""
    fields = loads(body)
    fields.update(updates)
    prose = _BLOCK.sub("", body or "")
    key = fields.get("approval_key")
    if isinstance(key, str):
        line = f"approval_key: {key}"
        kept = [part for part in prose.splitlines() if part.strip() != line]
        prose = "\n".join(kept)
    return dumps(fields, prose.strip())
