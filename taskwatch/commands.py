"""CLI behavior. Callers inject the board, the clock, and the version probe."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from .card_body import dumps
from .checks import (
    CheckInput,
    Decision,
    WatchTarget,
    format_cause,
    gate,
    internal_rewarn_due,
    received_at,
    user_notify_due,
    watch,
)
from .config import Config, config_path, load_config
from .destinations import DestinationError, parse_destination
from .pin import TESTED_HERMES_VERSION
from .protocol import BoardReader
from .state import State, Warning
from .units import profile_home, service_text, timer_text

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

SERVICE = "hermes-taskwatch-scan.service"
TIMER = "hermes-taskwatch-scan.timer"


class CommandError(ValueError):
    """The command cannot finish. Nothing partial is left running."""


class WarningWriter(Protocol):
    def create_blocked(self, title: str, body: str) -> str: ...

    def subscribe(self, card_id: str, destination: str) -> None: ...

    def subscribed(self, card_id: str, destination: str) -> bool: ...

    def complete(self, card_id: str, summary: str) -> bool: ...

    def archive(self, card_id: str) -> None: ...

    def statuses(self) -> dict[str, str]: ...


@dataclass(frozen=True, slots=True)
class Finding:
    name: str
    ok: bool
    detail: str
    warn: bool = False


@dataclass(frozen=True, slots=True)
class Probe:
    hermes_version: str | None
    nested_reopen: bool | None
    plugin_enabled: Callable[[str], bool | None]


def draft_config(root: str, board: str, profiles: tuple[str, ...]) -> dict:
    spec = {
        "platform": "placeholder",
        "chat_id": "chat.invalid",
        "thread_id": "0",
        "chat_type": "dm",
        "profile": profiles[0],
        "delivery_mode": "wake",
    }
    encoded = json.dumps(spec, separators=(",", ":"))
    return {
        "version": 1,
        "hermes_root": root,
        "board": board,
        "profiles": list(profiles),
        "watch_hook": False,
        "destinations": {"main_subscription": encoded, "user_notify": encoded},
        "executors": {
            "work": ["executor", "--brief", "{brief}", "--copy", "{copy}", "--output", "{output}"],
            "review": ["executor", "--review", "--brief", "{brief}", "--output", "{output}"],
        },
        "times": {
            "gate_action_seconds": 1800,
            "reject_followup_extra_seconds": 3600,
            "next_start_seconds": 1800,
            "unstarted_after_parents_seconds": 3600,
            "start_approval_without_card_seconds": 1800,
            "rewarn_initial_seconds": 1800,
            "rewarn_backoff": 2,
            "user_notify_unreceived_seconds": 600,
            "stall_unchanged_seconds": 1800,
        },
        "limits": {"reject_cap": 3, "rewarn_cap": 3},
        "comments": {
            "handled_prefix": "handled event=",
            "received_prefix": "received alert=",
            "approve": "approve",
            "reject": "reject",
            "wait_prefix": "wait:",
            "next_prefix": "next:",
        },
    }


def init_layout(
    root: Path,
    *,
    board: str,
    profiles: tuple[str, ...],
    hermes_exe: str,
    unit_dir: Path,
    runner: Callable[[list[str]], None],
) -> Path:
    if not profiles:
        raise CommandError("profiles must be a non-empty list")
    path = config_path(root)
    if path.exists():
        raise CommandError("config already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = draft_config(str(root.resolve()), board, profiles)
    path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    load_config(path)
    home = profile_home(str(root.resolve()), profiles[0])
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / SERVICE).write_text(service_text(hermes_exe, home), encoding="utf-8")
    (unit_dir / TIMER).write_text(timer_text(), encoding="utf-8")
    runner(["systemctl", "--user", "daemon-reload"])
    return path


def doctor_report(root: Path, board: BoardReader, probe: Probe) -> tuple[Finding, ...]:
    findings = [_config_finding(root)]
    config = _loaded(root) if findings[0].ok else None
    findings.append(_board_finding(board))
    findings.append(_destination_finding(config))
    findings.append(_write_finding(root))
    findings.append(_version_finding(probe.hermes_version))
    findings.append(_reject_finding(probe.nested_reopen))
    findings.extend(_plugin_findings(config, probe.plugin_enabled))
    return tuple(findings)


def scan_board(
    config: Config,
    board: BoardReader,
    state: State,
    writer: WarningWriter,
    now: datetime,
    target: WatchTarget,
    *,
    deliver: Callable[[str, str], None],
    record_error: str | None = None,
) -> tuple[str, ...]:
    """Warn the main agent, then tell the user when the checks say so.

    The user check runs on every scan. It reads the warning card's own subscription and
    received comments, so the rewarn interval cannot hide a lost or unanswered warning.
    """
    with state.lock():
        loaded = state.load()
        decision = watch(
            CheckInput(
                now=now,
                config=config,
                board=board,
                approvals=loaded.approvals,
                reviews=loaded.reviews,
                warnings=loaded.warnings,
                target=target,
            ),
            record_error=record_error or loaded.error,
        )
        lines = []
        for cause in decision.causes:
            if internal_rewarn_due(loaded.warnings, cause.cause_id, now, config):
                _write_warning(config, writer, cause, now, state)
            line = _notify_user(config, board, state, cause, now, deliver)
            if line:
                lines.append(line)
            loaded = state.load()
        return tuple(lines)


def _notify_user(config, board, state, cause, now, deliver) -> str | None:
    own = [card for card in board.cards() if card.warning_for == cause.cause_id]
    newest = max(own, key=lambda card: (card.done_at or _EPOCH, card.id), default=None)
    subscribed = (
        newest is not None and config.destinations.main_subscription in newest.subscriptions
    )
    loaded = state.load()
    latest = max(
        (
            item.warned_at
            for item in loaded.warnings
            if item.cause_id == cause.cause_id and item.channel == "internal"
        ),
        default=None,
    )
    # Only a reply on the latest warning card after the latest internal warning answers it.
    # Hermes stamps comments in whole seconds, so the warning time is cut to whole seconds too.
    told = None
    if latest is not None and newest is not None:
        floor = latest.replace(microsecond=0)
        replies = [item for item in newest.comments if item.at >= floor]
        told = received_at(replies, config.comments, cause.cause_id)
    if not user_notify_due(
        loaded.warnings, cause.cause_id, now, config, told, cause.summary, subscribed
    ):
        return None
    try:
        deliver(config.destinations.user_notify, format_cause(cause))
    except OSError as exc:
        return f"undelivered {cause.cause_id}: {exc}"
    state.append_warning(Warning(1, cause.cause_id, now, "user", cause.summary))
    return f"{config.destinations.user_notify} {cause.cause_id}"


def check_card(
    config: Config,
    board: BoardReader,
    state: State,
    card_id: str,
    now: datetime,
    *,
    action: str | None = None,
    apply: bool = False,
) -> Decision:
    loaded = state.load()
    card = None if loaded.error else board.card(card_id)
    chosen = "apply" if apply else (action or _movement(card))
    return gate(
        chosen,
        CheckInput(
            now=now,
            config=config,
            board=board,
            card_id=card_id,
            approvals=loaded.approvals,
            reviews=loaded.reviews,
            warnings=loaded.warnings,
            target=WatchTarget(config.hermes_root, config.board, config.profiles[0]),
        ),
        profile_in_list=True,
        board_matches=True,
        config_error=None,
        record_error=loaded.error,
    )


def format_decision(decision: Decision) -> str:
    if not decision.causes:
        return "pass\n"
    lines = [format_cause(cause) for cause in decision.causes]
    head = "block" if decision.block else "warning"
    return head + "\n" + "\n".join(lines) + "\n"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _loaded(root: Path) -> Config | None:
    try:
        return load_config(config_path(root))
    except Exception:
        return None


def _config_finding(root: Path) -> Finding:
    try:
        load_config(config_path(root))
    except Exception as exc:
        return Finding("config", False, str(exc))
    return Finding("config", True, "version 1")


def _board_finding(board: BoardReader) -> Finding:
    try:
        error = board.read_error()
        board.cards()
    except Exception as exc:
        return Finding("board", False, str(exc))
    if error:
        return Finding("board", False, error)
    return Finding("board", True, "readable")


def _destination_finding(config: Config | None) -> Finding:
    if config is None:
        return Finding("destinations", False, "config unread")
    try:
        parse_destination(config.destinations.main_subscription)
        parse_destination(config.destinations.user_notify)
    except DestinationError as exc:
        return Finding("destinations", False, str(exc))
    return Finding("destinations", True, "subscription specs")


def _write_finding(root: Path) -> Finding:
    try:
        state = State(root)
        with state.lock():
            probe = state.dir / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
    except OSError as exc:
        return Finding("write", False, str(exc))
    return Finding("write", True, "state directory")


def _version_finding(version: str | None) -> Finding:
    parsed = None if version is None else _version_tuple(version)
    tested = _version_tuple(TESTED_HERMES_VERSION)
    if parsed is None or tested is None:
        return Finding("hermes", False, "version unreadable")
    if parsed < tested:
        return Finding("hermes", False, f"tested {TESTED_HERMES_VERSION}, running {version}")
    if parsed > tested:
        return Finding(
            "hermes", True, f"tested {TESTED_HERMES_VERSION}, running {version}", warn=True
        )
    return Finding("hermes", True, version or TESTED_HERMES_VERSION)


def _version_tuple(version: str) -> tuple[int, ...] | None:
    parts = version.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _reject_finding(available: bool | None) -> Finding:
    if available is None:
        return Finding("reject", False, "capability unreadable")
    if not available:
        return Finding("reject", False, "core update needed")
    return Finding("reject", True, "nested reopen")


def _plugin_findings(
    config: Config | None, enabled: Callable[[str], bool | None]
) -> tuple[Finding, ...]:
    if config is None:
        return (Finding("plugin", False, "config unread"),)
    findings = []
    for profile in config.profiles:
        state = enabled(profile)
        if state is None:
            findings.append(Finding(f"plugin:{profile}", False, "unreadable"))
        elif not state:
            findings.append(Finding(f"plugin:{profile}", False, "disabled"))
        else:
            findings.append(Finding(f"plugin:{profile}", True, "enabled"))
    return tuple(findings)


def _write_warning(
    config: Config, writer: WarningWriter, cause, now: datetime, state: State
) -> bool:
    body = dumps({"warning_for": cause.cause_id}, format_cause(cause))
    before = set(writer.statuses())
    try:
        card_id = writer.create_blocked(cause.cause_id, body)
        writer.subscribe(card_id, config.destinations.main_subscription)
        if not writer.subscribed(card_id, config.destinations.main_subscription):
            writer.archive(card_id)
            return False
        if not writer.complete(card_id, f"{cause.cause_id} {cause.summary}"):
            writer.archive(card_id)
            return False
    except OSError:
        for card_id in set(writer.statuses()) - before:
            writer.archive(card_id)
        return False
    state.append_warning(Warning(1, cause.cause_id, now, "internal", cause.summary))
    return True


def _movement(card) -> str:
    if card is not None and card.gate and card.status == "review":
        return "approve"
    return "complete"
