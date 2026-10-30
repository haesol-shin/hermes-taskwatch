"""The checklist file a job keeps. A problem is an item that does not pass."""

import json
from pathlib import Path


def checklist_problems(path: str | None) -> tuple[str, ...] | None:
    if not path or not Path(path).is_file():
        return ("missing",)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    items = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        return None
    failing = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("passes"), bool):
            return None
        if not item["passes"]:
            failing.append(str(item.get("id", "item")))
    return tuple(failing)
