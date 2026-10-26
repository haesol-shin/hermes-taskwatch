"""Hermes tool handlers. The board writer is injected; Hermes is not imported."""

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .cards import create_cards, reject_gate
from .checks import CheckInput, apply_match, clean_result
from .config import config_path, load_config
from .evidence import EvidenceBoard
from .hermes_board import hermes_root
from .protocol import BoardReadError
from .state import Approval, RecordError, State, StateError

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
    "description": "Reject a gate. A review gate is not supported.",
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

APPROVE_SCHEMA = {
    "name": "taskwatch_approve",
    "description": (
        "Record a start or apply approval only after the user approved in the conversation. "
        "Use a stable approval_key derived from the user's message id for start. "
        "Apply requires the card's current result fingerprint."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["start", "apply"]},
            "approval_key": {"type": "string"},
            "approved": {"type": "string"},
            "message_id": {"type": "string"},
            "card_id": {"type": "string"},
            "fingerprint": {"type": "string"},
        },
        "required": ["kind", "approved", "message_id"],
    },
}


def taskwatch_approve(args, writer=None, **kwargs):
    del kwargs
    state = State(hermes_root())
    try:
        with state.lock():
            result = _approve_locked(args, state, writer)
    except (KeyError, ValueError, OSError, BoardReadError) as exc:
        result = {"ok": False, "error": str(exc)}
    return json.dumps(result)


def _approve_locked(args, state, writer):
    kind = args["kind"]
    if kind not in {"start", "apply"}:
        raise StateError("unknown approval kind")
    records = state.load()
    if records.error:
        raise RecordError(records.error)
    now = datetime.now(timezone.utc)
    if kind == "start":
        existing = next(
            (
                item
                for item in records.approvals
                if item.kind == "start" and item.approval_key == args["approval_key"]
            ),
            None,
        )
        if existing is None:
            state.append_start(args["approval_key"], now, args["approved"], args["message_id"])
    else:
        board = _writer(writer)
        if board is None:
            raise StateError("board adapter missing")
        board = EvidenceBoard(board, state)
        error = board.read_error()
        if error:
            raise BoardReadError(error)
        config = load_config(config_path(state.root))
        candidate = Approval(
            1,
            "apply",
            None,
            approved=args["approved"],
            message_id=args["message_id"],
            card_id=args["card_id"],
            fingerprint=args["fingerprint"],
        )
        inp = CheckInput(now, config, board, card_id=args["card_id"], approvals=(candidate,))
        causes = clean_result(inp) + apply_match(inp)
        card = board.card(args["card_id"])
        if causes or card is None or not card.fingerprint:
            raise StateError(causes[0].summary if causes else "result fingerprint missing")
        existing = next(
            (
                item
                for item in records.approvals
                if item.kind == "apply"
                and item.message_id == args["message_id"]
                and item.card_id == args["card_id"]
                and item.fingerprint == args["fingerprint"]
            ),
            None,
        )
        if existing is None:
            state.append_apply(
                args["approved"], args["message_id"], args["card_id"], args["fingerprint"], now
            )
    approval = existing or state.load().approvals[-1]
    payload = asdict(approval)
    if approval.approved_at is not None:
        payload["approved_at"] = approval.approved_at.isoformat()
    return {"ok": True, "approval": payload}


def taskwatch_create(args, writer=None, **kwargs):
    del kwargs
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


def taskwatch_reject(args, writer=None, **kwargs):
    del kwargs
    writer = _writer(writer)
    if writer is None:
        return json.dumps({"ok": False, "error": "board adapter missing"})
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
