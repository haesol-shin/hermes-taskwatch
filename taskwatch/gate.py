"""Block kanban_complete and kanban_request_review when the checks fail.

The hook's task_id is an execution id. The card id is args.task_id, then
HERMES_KANBAN_TASK. A profile outside the list, or another board database, is
not a target. An unresolved target inside the list blocks.
"""

import os
from datetime import datetime
from pathlib import Path
from typing import Callable

from .checks import CheckInput, Decision, WatchTarget, format_cause, gate
from .commands import utcnow
from .config import Config, ConfigError, load_config
from .protocol import BoardReader, BoardReadError
from .state import State

_TOOLS = {"kanban_complete": "complete", "kanban_request_review": "request_review"}


def decide(
    *,
    tool_name: str,
    args: dict | None,
    profile_name: str | None,
    env_task: str | None,
    db_path: str | None,
    config: Config | None,
    config_error: str | None,
    board: BoardReader | None,
    state: State | None,
    now: datetime,
    freezer: Callable | None = None,
    record_snapshot: Callable[[str, str], None] | None = None,
    configured_db: str | None = None,
) -> dict | None:
    if tool_name not in _TOOLS:
        return None
    skipped = _outside(profile_name, db_path, config, config_error, configured_db)
    if skipped is True:
        return None
    if skipped:
        return _block(skipped)
    assert config is not None and board is not None and state is not None
    card_id = _card_id(args, env_task)
    if not card_id or db_path is None:
        return _block("unresolved target")
    loaded = state.load()
    card = None
    if not loaded.error:
        try:
            card = board.card(card_id)
        except (OSError, BoardReadError) as exc:
            return _block(str(exc))
    action = _action(tool_name, card)
    decision = gate(
        action,
        CheckInput(
            now=now,
            config=config,
            board=board,
            card_id=card_id,
            card=card,
            card_loaded=not loaded.error,
            approvals=loaded.approvals,
            reviews=loaded.reviews,
            warnings=loaded.warnings,
            target=WatchTarget(config.hermes_root, config.board, profile_name),
        ),
        profile_in_list=True,
        board_matches=True,
        record_error=loaded.error,
    )
    if decision.block or not decision.checked:
        return _block(_message(decision))
    if action == "approve":
        failed = _freeze(card, state, freezer, record_snapshot)
        if failed:
            return _block(failed)
    return None


def load_gate_config(path: Path) -> tuple[Config | None, str | None]:
    try:
        return load_config(path), None
    except ConfigError as exc:
        return None, str(exc)


def _outside(profile, db_path, config, error, configured_db):
    if error or config is None:
        return error or "unresolved target"
    if profile and profile not in config.profiles:
        return True
    if not profile or not db_path or not configured_db:
        return "unresolved target"
    if db_path != configured_db:
        return True
    return False


def pre_tool_call(tool_name: str, args: dict | None = None, task_id: str = "", **kwargs):
    """Hermes hook. ``task_id`` is the execution id and is not the card id."""
    del task_id
    if "board" not in kwargs:
        if tool_name not in _TOOLS:
            return None
        try:
            kwargs = {**kwargs, **gate_context(args, kwargs)}
        except Exception as exc:
            return _block(f"unresolved target: {exc}")
    profile = kwargs.get("profile_name")
    if profile is None and "ctx" in kwargs:
        profile = getattr(kwargs["ctx"], "profile_name", None)
    return decide(
        tool_name=tool_name,
        args=args,
        profile_name=profile,
        env_task=kwargs.get("env_task"),
        db_path=kwargs.get("db_path"),
        config=kwargs.get("config"),
        config_error=kwargs.get("config_error"),
        board=kwargs.get("board"),
        state=kwargs.get("state"),
        now=kwargs.get("now") or utcnow(),
        freezer=kwargs.get("freezer"),
        record_snapshot=kwargs.get("record_snapshot"),
        configured_db=kwargs.get("configured_db"),
    )


def gate_context(args: dict | None, kwargs: dict) -> dict:
    """Fill the gate from the installed Hermes when the caller did not inject a board."""
    from .config import config_path
    from .evidence import EvidenceBoard
    from .fingerprint import freeze
    from .hermes_board import configured_db_path, db_path, hermes_root, open_board
    from .state import State

    root = kwargs.get("root") or hermes_root()
    config, error = load_gate_config(config_path(root))
    profile = kwargs.get("profile_name")
    if profile is None and "ctx" in kwargs:
        profile = getattr(kwargs["ctx"], "profile_name", None)
    if "env_task" in kwargs:
        env_task = kwargs.get("env_task")
    else:
        env_task = os.environ.get("HERMES_KANBAN_TASK")
    board_arg = args.get("board") if isinstance(args, dict) else None
    # The database this call really opens. A name in the arguments does not pick another one.
    seen = db_path(board_arg)
    configured = None if config is None else configured_db_path(config.board)
    raw = open_board(db_path=seen)
    state = State(root)
    reader = EvidenceBoard(raw, state)

    def freezer(card, state_dir):
        result = reader.result(card)
        if result is None:
            raise OSError("no run record for the approved result")
        return freeze(result, state_dir)

    return {
        "profile_name": profile,
        "env_task": env_task,
        "db_path": seen,
        "config": config,
        "config_error": error,
        "board": reader,
        "state": state,
        "freezer": freezer,
        "record_snapshot": raw.record_snapshot,
        "configured_db": configured,
    }


def _card_id(args: dict | None, env_task: str | None) -> str | None:
    if isinstance(args, dict):
        raw = args.get("task_id")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    if env_task and env_task.strip():
        return env_task.strip()
    return None


def _action(tool_name: str, card) -> str:
    if tool_name == "kanban_complete" and card is not None and card.gate:
        return "approve"
    return _TOOLS[tool_name]


def _freeze(card, state: State, freezer, record_snapshot) -> str | None:
    if card is None or not card.fingerprint or freezer is None or record_snapshot is None:
        return "cannot freeze the approved result"
    try:
        path = freezer(card, state.dir)
    except (OSError, ValueError) as exc:
        return str(exc)
    # The snapshot is named by what was frozen. It must be what the gate just checked.
    if path.name != card.fingerprint:
        return "the result changed while it was frozen"
    record_snapshot(card.id, str(path))
    return None


def _message(decision: Decision) -> str:
    if not decision.causes:
        return "uncheckable"
    return format_cause(decision.causes[0]).replace("\n", " ")


def _block(message: str) -> dict[str, str]:
    return {"action": "block", "message": message}
