"""argparse tree for `hermes taskwatch`. The handler prints and returns text."""

import argparse
import sys

from .checks import WatchTarget
from .commands import (
    CommandError,
    check_card,
    doctor_report,
    format_decision,
    init_layout,
    scan_board,
    utcnow,
)
from .config import ConfigError, config_path, hermes_root, load_config
from .evidence import EvidenceBoard
from .protocol import BoardReadError
from .state import State
from .units import UnitError


def setup_cli(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="taskwatch_command", required=True)
    init = subs.add_parser("init", help="Write a config draft and the scan timer")
    init.add_argument("--root")
    init.add_argument("--board", default="default")
    init.add_argument("--profile", action="append", dest="profiles")
    init.add_argument("--hermes", required=True)
    init.add_argument("--unit-dir")
    doctor = subs.add_parser("doctor", help="Check config, board, destinations, and Hermes")
    doctor.add_argument("--root")
    scan = subs.add_parser("scan", help="Scan the board once")
    scan.add_argument("--root")
    check = subs.add_parser("check", help="Show whether a card can move on")
    check.add_argument("card")
    check.add_argument("--root")


def handle_cli(args: argparse.Namespace, deps: dict | None = None) -> str:
    deps = deps or {}
    try:
        text, code = _run(args, deps)
    except (CommandError, ConfigError, UnitError, OSError, BoardReadError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    if text:
        print(text, end="" if text.endswith("\n") else "\n")
    if code:
        raise SystemExit(code)
    return text


def _run(args: argparse.Namespace, deps: dict) -> tuple[str, int]:
    command = args.taskwatch_command
    if command == "init":
        return _init(args, deps), 0
    if command == "doctor":
        return _doctor(args, deps)
    if command == "scan":
        return _scan(args, deps), 0
    if command == "check":
        return _check(args, deps)
    raise CommandError(f"unknown command {command}")


def _root(args: argparse.Namespace) -> object:
    raw = getattr(args, "root", None)
    if raw:
        from pathlib import Path

        return Path(raw)
    return hermes_root()


def _init(args: argparse.Namespace, deps: dict) -> str:
    from pathlib import Path

    profiles = tuple(args.profiles or ["default"])
    unit_dir = Path(args.unit_dir) if args.unit_dir else _user_unit_dir()
    runner = deps.get("runner", _reload)
    path = init_layout(
        Path(args.root) if getattr(args, "root", None) else hermes_root(),
        board=args.board,
        profiles=profiles,
        hermes_exe=args.hermes,
        unit_dir=unit_dir,
        runner=runner,
    )
    return f"wrote {path}\n"


def _doctor(args: argparse.Namespace, deps: dict) -> tuple[str, int]:
    root = _root(args)
    board = deps.get("board")
    if board is None:
        board = _open_board(_configured_board(root))
    probe = deps.get("probe")
    if probe is None:
        from .hermes_board import live_probe

        probe = live_probe(root)
    findings = doctor_report(root, board, probe)
    lines = [_finding_line(item) for item in findings]
    return "\n".join(lines) + "\n", 0 if all(item.ok for item in findings) else 1


def _scan(args: argparse.Namespace, deps: dict) -> str:
    root = _root(args)
    config = load_config(config_path(root))
    # Hermes takes `--profile` itself, so the profile is the one this process runs in.
    profile = deps.get("profile") or _current_profile()
    if profile is None:
        raise CommandError("the active Hermes profile is unknown")
    state = State(root)
    board, writer = _board_and_writer(deps, config.board, state)
    lines = scan_board(
        config,
        board,
        state,
        writer,
        deps.get("now", utcnow()),
        WatchTarget(str(root.resolve()), config.board, profile),
        deliver=deps.get("deliver") or _live_deliver(),
    )
    return "\n".join(lines) + ("\n" if lines else "")


def _check(args: argparse.Namespace, deps: dict) -> tuple[str, int]:
    root = _root(args)
    config = load_config(config_path(root))
    state = State(root)
    board = deps.get("board") or _evidence_board(config.board, state)
    decision = check_card(
        config,
        board,
        state,
        args.card,
        deps.get("now", utcnow()),
    )
    code = 1 if decision.block or not decision.checked else 0

    return format_decision(decision), code


def _finding_line(item) -> str:
    label = "warn" if item.warn else ("ok" if item.ok else "fail")
    return f"{item.name}: {label} {item.detail}"


def _configured_board(root) -> str | None:
    try:
        return load_config(config_path(root)).board
    except ConfigError:
        return None


def _board_and_writer(deps: dict, slug: str, state: State):
    board = deps.get("board")
    writer = deps.get("writer")
    if board is not None and writer is not None:
        return board, writer
    live = _open_board(slug)
    return board or EvidenceBoard(live, state), writer or live


def _evidence_board(slug: str, state: State):
    # The board reads the same State the command holds. A second State would wait on
    # the first one's file lock forever.
    return EvidenceBoard(_open_board(slug), state)


def _current_profile() -> str | None:
    from .hermes_board import _active_profile, _plugin_profile

    return _plugin_profile() or _active_profile()


def _live_deliver():
    from .hermes_board import deliver_user

    return deliver_user


def _open_board(slug: str | None):
    from .hermes_board import open_board

    return open_board(slug)


def _reload(argv: list[str]) -> None:
    import subprocess

    subprocess.run(argv, check=False)


def _user_unit_dir():
    from pathlib import Path

    return Path.home() / ".config" / "systemd" / "user"
