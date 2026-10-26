"""Hermes tool handlers. The board writer is injected; Hermes is not imported."""

import json
from datetime import datetime, timezone
from pathlib import Path

from .cards import create_cards, reject_gate
from .config import config_path, hermes_root, load_config
from .state import State

CREATE_SCHEMA = {
    "name": "taskwatch_create",
    "description": "Create cards from an intent file and a spec file after a start approval.",
    "parameters": {
        "type": "object",
        "properties": {
            "approval_key": {"type": "string"},
            "intent": {"type": "string"},
            "spec": {"type": "string"},
            "cards": {"type": "array"},
        },
        "required": ["approval_key", "intent", "spec", "cards"],
    },
}
REJECT_SCHEMA = {
    "name": "taskwatch_reject",
    "description": "Reject a gate. A review gate needs nested reopen in Hermes.",
    "parameters": {
        "type": "object",
        "properties": {
            "card_id": {"type": "string"},
            "reason": {"type": "string"},
            "reject_id": {"type": "string"},
        },
        "required": ["card_id", "reason", "reject_id"],
    },
}


def taskwatch_create(args, writer=None, nested_reopen=None, **kwargs):
    del nested_reopen, kwargs
    writer = _writer(writer)
    if writer is None:
        return json.dumps({"ok": False, "error": "board adapter missing"})
    root = hermes_root()
    config = load_config(config_path(root))
    result = create_cards(
        config,
        State(root),
        writer,
        approval_key=args["approval_key"],
        intent=Path(args["intent"]),
        spec=Path(args["spec"]),
        cards=tuple(args["cards"]),
        now=datetime.now(timezone.utc),
    )
    return json.dumps(result)


def taskwatch_reject(args, writer=None, nested_reopen=None, **kwargs):
    del kwargs
    writer = _writer(writer)
    if writer is None:
        return json.dumps({"ok": False, "error": "board adapter missing"})
    if nested_reopen is None:
        from .hermes_board import nested_reopen_available

        nested_reopen = nested_reopen_available() is True
    root = hermes_root()
    config = load_config(config_path(root))
    result = reject_gate(
        config,
        State(root),
        writer,
        card_id=args["card_id"],
        reason=args["reason"],
        reject_id=args["reject_id"],
        now=datetime.now(timezone.utc),
        nested_reopen=nested_reopen,
    )
    return json.dumps(result)


def _writer(writer):
    if writer is not None:
        return writer
    try:
        from .hermes_board import open_board

        config = load_config(config_path(hermes_root()))
    except Exception:
        return None
    return open_board(config.board)
