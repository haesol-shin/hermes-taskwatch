"""Run the configured executor in the foreground and print one JSON line.

The Kanban worker waits for this process. It must not use a background notice.
Stdout is the JSON line and nothing else; the executor's own output goes to a log file
under the state directory, and the path is in the line.

The script writes the run record the gate reads: exit code, result files, checklist, and
the copy that holds the result. A card whose parent gate was approved gets its own work
copy made from that approved snapshot, and the executor, the checks, and the record all
use that copy. A card behind a gate with no approved snapshot is blocked.

Run it with the Python that can import Hermes, so it can read the card.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# The plugin root holds the `taskwatch` package. The script runs from an installed plugin.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from taskwatch.config import config_path, hermes_root, load_config  # noqa: E402
from taskwatch.executor import run_executor  # noqa: E402
from taskwatch.protocol import BoardReadError  # noqa: E402
from taskwatch.state import State  # noqa: E402


def main(argv: list[str] | None = None, board=None) -> int:
    args = _parser().parse_args(argv)
    result = _run(args, board)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a taskwatch executor template")
    parser.add_argument("--config")
    parser.add_argument("--kind", choices=("work", "review"), required=True)
    parser.add_argument("--card", help="default: HERMES_KANBAN_TASK")
    parser.add_argument("--brief", required=True)
    parser.add_argument("--copy", required=True, help="the work copy that holds the result")
    parser.add_argument("--output", required=True)
    parser.add_argument("--result", action="append", default=[], help="required file under copy")
    parser.add_argument("--git", action="store_true")
    parser.add_argument("--sha", help="the commit under review; it must be the copy's HEAD")
    parser.add_argument("--guideline", action="append", default=[])
    parser.add_argument("--checklist")
    parser.add_argument("--progress")
    return parser


def _run(args: argparse.Namespace, board) -> dict:
    config = load_config(Path(args.config) if args.config else config_path(hermes_root()))
    card_id = args.card or os.environ.get("HERMES_KANBAN_TASK")
    if not card_id:
        return _block("card id is missing")
    if board is None:
        from taskwatch.hermes_board import open_board

        board = open_board(config.board)
    copy = os.path.abspath(args.copy)
    try:
        card = board.card(card_id)
        if card is None:
            return _block(f"unknown card {card_id}")
        snapshot = _approved_snapshot(board, card) if args.kind == "work" else None
        if snapshot is not None:
            _work_copy_from(snapshot, Path(copy))
    except (OSError, BoardReadError, ValueError) as exc:
        return _block(str(exc))
    if not card.intent_path or not card.spec_path:
        return _block("the card names no intent or spec")
    result = run_executor(
        config,
        kind=args.kind,
        card=card_id,
        brief=args.brief,
        copy=copy,
        output=args.output,
        now=datetime.now(timezone.utc),
        state=State(Path(config.hermes_root)),
        runner=_to_log,
        results=tuple(args.result),
        git=args.git,
        sha=args.sha,
        intent=Path(card.intent_path),
        spec=Path(card.spec_path),
        guidelines=tuple(Path(item) for item in args.guideline),
        checklist=args.checklist,
        progress=args.progress,
    )
    result["card"] = card_id
    return result


def _approved_snapshot(board, card) -> str | None:
    """The frozen copy a parent gate approved, or None when no parent is a gate.

    A parent gate without a snapshot, or a parent that cannot be read, stops the card:
    it must not run on a copy nobody approved.
    """
    found = set()
    for parent_id in card.parents:
        parent = board.card(parent_id)
        if parent is None:
            raise ValueError(f"cannot read the parent card {parent_id}")
        if not parent.gate:
            continue
        if not parent.snapshot_path:
            raise ValueError(f"the parent gate {parent_id} has no approved snapshot")
        found.add(parent.snapshot_path)
    if len(found) > 1:
        raise ValueError("more than one approved snapshot reaches this card")
    if not found:
        return None
    path = found.pop()
    if not Path(path).is_dir():
        raise ValueError(f"the approved snapshot is missing: {path}")
    return path


def _work_copy_from(snapshot: str, copy: Path) -> None:
    """Give the card its own work copy of the snapshot. The snapshot is only read.

    A copy that is already there is moved aside, not deleted: it may hold earlier work.
    """
    source = Path(snapshot).resolve()
    if copy.resolve() == source or source in copy.resolve().parents:
        raise ValueError("the work copy must not be the approved snapshot or inside it")
    if copy.exists():
        copy.rename(copy.with_name(f"{copy.name}.before-{uuid.uuid4().hex[:8]}"))
    shutil.copytree(source, copy, symlinks=True)


def _to_log(argv: list[str], log: Path) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("wb") as handle:
        try:
            completed = subprocess.run(
                argv, check=False, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT
            )
        except OSError as exc:
            handle.write(f"cannot start the executor: {exc}\n".encode())
            return 127
    return completed.returncode


def _block(reason: str) -> dict:
    return {"ok": False, "block": reason}


if __name__ == "__main__":
    sys.exit(main())
