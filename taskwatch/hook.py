"""Dispatch-tick watch. Off unless config version 1 sets watch_hook true.

A missing config stays quiet. A present config that cannot be read is not
treated as off: the caller gets the error so a broken file is not silent.
"""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .checks import WatchTarget
from .commands import CommandError, WarningWriter, scan_board, scan_errors, utcnow
from .config import ConfigError, config_path, load_config
from .evidence import EvidenceBoard
from .hermes_board import hermes_root
from .protocol import BoardReader
from .state import State


def on_dispatch_tick(
    *,
    root: Path,
    board_name: str | None,
    profile_name: str | None,
    reader: BoardReader,
    writer: WarningWriter,
    now: datetime,
    deliver: Callable[[str, str], None],
    state: State | None = None,
) -> tuple[str, ...]:
    path = config_path(root)
    if not path.exists():
        return ()
    try:
        config = load_config(path)
    except ConfigError as exc:
        return (str(exc),)
    if not config.watch_hook:
        return ()
    # An omitted board cannot be confirmed. Empty is not the configured board.
    seen = board_name if board_name else ""
    target = WatchTarget(str(root.resolve()), seen, profile_name)
    state = state or State(root)
    try:
        with scan_errors(config, state, now, deliver):
            return scan_board(config, reader, state, writer, now, target, deliver=deliver)
    except CommandError as exc:
        return (str(exc),)


def dispatch_tick(
    board: str | None = None,
    profile_name: str | None = None,
    reader: BoardReader | None = None,
    writer: WarningWriter | None = None,
    now: datetime | None = None,
    root: Path | None = None,
    deliver: Callable[[str, str], None] | None = None,
    **_ignored: object,
) -> tuple[str, ...]:
    root = root or hermes_root()
    # The evidence board and the scan share one State; a second one would wait on the lock.
    state = State(root)
    if reader is None or writer is None:
        from .hermes_board import open_board

        try:
            live = open_board(board)
        except Exception as exc:
            return (str(exc),)
        reader = reader or EvidenceBoard(live, state)
        writer = writer or live
    if deliver is None:
        from .hermes_board import deliver_user

        deliver = deliver_user
    return on_dispatch_tick(
        root=root,
        board_name=board,
        profile_name=profile_name,
        reader=reader,
        writer=writer,
        now=now or utcnow(),
        deliver=deliver,
        state=state,
    )
