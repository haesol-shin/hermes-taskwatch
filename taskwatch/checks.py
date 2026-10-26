"""Pure checks. Time is an argument. The board is only read through BoardReader.

Each check returns causes. It does not change a card. A read failure is a
cause, not an empty result.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import CommentRules, Config
from .protocol import BoardReader, BoardReadError, Card, Comment, Event
from .state import Approval, Review, Warning

WORK_KINDS = frozenset({"review_requested"})
IGNORED_KINDS = frozenset({"created", "warning"})
FAILURE_STATUSES = frozenset(
    {"blocked", "triage", "crashed", "gave_up", "timed_out", "block_loop_detected"}
)
GATE_ACTIONS = {
    "complete": ("worker_finish",),
    "request_review": ("worker_finish",),
    "approve": ("worker_finish", "pass_review", "clean_result"),
    "apply": ("worker_finish", "pass_review", "clean_result", "apply_match"),
}


@dataclass(frozen=True, slots=True)
class Cause:
    check: str
    cause_id: str
    summary: str
    next_action: str
    certain: bool
    uncheckable: bool = False


@dataclass(frozen=True, slots=True)
class Decision:
    checked: bool
    block: bool
    causes: tuple[Cause, ...]


@dataclass(frozen=True, slots=True)
class WatchTarget:
    hermes_root: str
    board: str
    profile: str | None = None


@dataclass(frozen=True, slots=True)
class Handled:
    event_id: str
    action: str
    argument: str
    at: datetime


@dataclass(frozen=True, slots=True)
class CheckInput:
    now: datetime
    config: Config
    board: BoardReader
    card_id: str | None = None
    approvals: tuple[Approval, ...] = ()
    reviews: tuple[Review, ...] = ()
    warnings: tuple[Warning, ...] = ()
    target: WatchTarget | None = None


@dataclass(frozen=True, slots=True)
class CheckSpec:
    name: str
    kind: str
    blocks: str
    removable_when: str
    run: Callable[[CheckInput], tuple[Cause, ...]]


REGISTRY: dict[str, CheckSpec] = {}


def watch(inp: CheckInput, *, record_error: str | None = None) -> Decision:
    if inp.target is None or _mismatch(inp.config, inp.target):
        return Decision(False, False, (_cause_mismatch(inp),))
    try:
        error = record_error or inp.board.read_error()
        if error:
            return _uncheckable_decision("watch", error, block=False)
        causes = _run_kind(inp, "watch")
    except (OSError, BoardReadError) as exc:
        return _uncheckable_decision("watch", str(exc), block=False)
    return Decision(True, False, causes)


def gate(
    action: str,
    inp: CheckInput,
    *,
    profile_in_list: bool | None,
    board_matches: bool | None,
    config_error: str | None = None,
    record_error: str | None = None,
) -> Decision:
    skipped = _gate_skip(profile_in_list, board_matches, config_error, record_error)
    if skipped is not None:
        return skipped
    try:
        error = inp.board.read_error()
        if error:
            return _uncheckable_decision("gate", error, block=True)
        if action not in GATE_ACTIONS:
            return _uncheckable_decision("gate", "unknown action", block=True)
        causes: list[Cause] = []
        for name in GATE_ACTIONS[action]:
            causes.extend(REGISTRY[name].run(inp))
    except (OSError, BoardReadError) as exc:
        return _uncheckable_decision("gate", str(exc), block=True)
    return Decision(True, bool(causes), tuple(causes))


def format_cause(cause: Cause) -> str:
    mark = "certain" if cause.certain else "uncertain"
    return f"{cause.cause_id} {cause.summary}\nnext: {cause.next_action}\ncertainty: {mark}"


def internal_rewarn_due(
    warnings: Sequence[Warning], cause_id: str, now: datetime, config: Config
) -> bool:
    internal = _warnings(warnings, cause_id, "internal")
    if not internal:
        return True
    if len(internal) >= config.limits.rewarn_cap:
        return False
    grown = config.times.rewarn_backoff ** (len(internal) - 1)
    delay = config.times.rewarn_initial_seconds * grown
    return now >= internal[-1].warned_at + timedelta(seconds=delay)


def user_notify_due(
    warnings: Sequence[Warning],
    cause_id: str,
    now: datetime,
    config: Config,
    received_at: datetime | None,
    summary: str,
    subscribed: bool = True,
) -> bool:
    internal = _warnings(warnings, cause_id, "internal")
    if not internal:
        return False
    quiet = timedelta(seconds=config.times.user_notify_unreceived_seconds)
    unreceived = received_at is None and now >= internal[-1].warned_at + quiet
    capped = len(internal) >= config.limits.rewarn_cap
    if subscribed and not unreceived and not capped:
        return False
    told = _warnings(warnings, cause_id, "user")
    return not told or summary != told[-1].summary


def received_at(comments: Sequence[Comment], rules: CommentRules, cause_id: str) -> datetime | None:
    expected = f"{rules.received_prefix}{cause_id}"
    times = [item.at for item in comments if item.text.strip() == expected]
    return min(times) if times else None


def _register(spec: CheckSpec) -> None:
    REGISTRY[spec.name] = spec


def _run_kind(inp: CheckInput, kind: str) -> tuple[Cause, ...]:
    causes: list[Cause] = []
    for spec in REGISTRY.values():
        if spec.kind == kind:
            causes.extend(spec.run(inp))
    return tuple(causes)


def _gate_skip(
    profile_in_list: bool | None,
    board_matches: bool | None,
    config_error: str | None,
    record_error: str | None,
) -> Decision | None:
    if profile_in_list is False or board_matches is False:
        return Decision(False, False, ())
    if config_error or record_error or profile_in_list is None or board_matches is None:
        reason = config_error or record_error or "unresolved target"
        return _uncheckable_decision("gate", reason, block=True)
    return None


def _mismatch(config: Config, target: WatchTarget) -> bool:
    if target.hermes_root != config.hermes_root or target.board != config.board:
        return True
    return target.profile is not None and target.profile not in config.profiles


def _cause_mismatch(inp: CheckInput) -> Cause:
    return _cause(
        "target_mismatch",
        "target",
        "watcher target does not match config",
        "Point the watcher at the configured root, board, and profile",
    )


def _uncheckable_decision(check: str, reason: str, *, block: bool) -> Decision:
    cause = _cause(
        check,
        f"{check}:unreadable",
        reason,
        "Fix the unreadable config, board, or record before trusting this result",
        certain=False,
        uncheckable=True,
    )
    return Decision(False, block, (cause,))


def _cause(
    check: str,
    cause_id: str,
    summary: str,
    next_action: str,
    *,
    certain: bool = True,
    uncheckable: bool = False,
) -> Cause:
    return Cause(check, cause_id, summary, next_action, certain, uncheckable)


def _need(inp: CheckInput) -> Card | Cause:
    if not inp.card_id:
        return _cause(
            "gate",
            "gate:card",
            "card is unresolved",
            "Pass the card id before completing or approving",
            certain=False,
            uncheckable=True,
        )
    try:
        card = inp.board.card(inp.card_id)
    except (OSError, BoardReadError) as exc:
        raise BoardReadError(str(exc)) from exc
    if card is None:
        return _cause(
            "gate",
            f"gate:{inp.card_id}",
            "card is missing",
            "Reload the card before completing or approving",
            certain=False,
            uncheckable=True,
        )
    return card


def _cards(inp: CheckInput) -> tuple[Card, ...]:
    if inp.board.read_error():
        raise BoardReadError(inp.board.read_error())
    # A warning card is taskwatch's own output. It is not work to check.
    return tuple(card for card in inp.board.cards() if card.warning_for is None)


def worker_finish(inp: CheckInput) -> tuple[Cause, ...]:
    card = _need(inp)
    if isinstance(card, Cause):
        return (card,)
    causes: list[Cause] = []
    if card.exit_code != 0:
        causes.append(
            _cause(
                "worker_finish",
                f"{card.id}:exit",
                f"exit code is {card.exit_code}",
                "Record a zero exit before completing",
            )
        )
    missing = [path for path in card.required_outputs if not Path(path).is_file()]
    if missing:
        causes.append(
            _cause(
                "worker_finish",
                f"{card.id}:outputs",
                "required output is missing",
                "Write every required output before completing",
            )
        )
    problems = checklist_problems(card.checklist_path)
    if problems is None:
        causes.append(
            _cause(
                "worker_finish",
                f"{card.id}:checklist",
                "checklist is unreadable",
                "Repair the checklist JSON before completing",
                certain=False,
                uncheckable=True,
            )
        )
    elif problems:
        causes.append(
            _cause(
                "worker_finish",
                f"{card.id}:checklist",
                "checklist has a failing item",
                "Make every checklist item pass before completing",
            )
        )
    return tuple(causes)


def pass_review(inp: CheckInput) -> tuple[Cause, ...]:
    card = _need(inp)
    if isinstance(card, Cause):
        return (card,)
    if _has_pass(inp.reviews, card):
        return ()
    return (
        _cause(
            "pass_review",
            f"{card.id}:review",
            "no PASS for the current fingerprint and basis",
            "Review the current fingerprint and basis before approving",
        ),
    )


def clean_result(inp: CheckInput) -> tuple[Cause, ...]:
    card = _need(inp)
    if isinstance(card, Cause):
        return (card,)
    if not card.dirty:
        return ()
    return (
        _cause(
            "clean_result",
            f"{card.id}:dirty",
            "work copy changed after the PASS",
            "Drop the uncommitted changes or review the new result",
        ),
    )


def apply_match(inp: CheckInput) -> tuple[Cause, ...]:
    card = _need(inp)
    if isinstance(card, Cause):
        return (card,)
    if any(_apply_matches(item, card) for item in inp.approvals):
        return ()
    return (
        _cause(
            "apply_match",
            f"{card.id}:apply",
            "apply approval does not match the current fingerprint",
            "Ask for a new apply approval of the current fingerprint",
        ),
    )


def gate_action(inp: CheckInput) -> tuple[Cause, ...]:
    limit = timedelta(seconds=inp.config.times.gate_action_seconds)
    causes = []
    for card in _cards(inp):
        if not card.gate:
            continue
        event = _latest_work_event(card)
        if event is None or inp.now < event.at + limit or _acted(card, event, inp):
            continue
        causes.append(
            _cause(
                "gate_action",
                f"{card.id}:{event.id}",
                "no real action after the latest review request",
                "Record done, start a reject, or keep a valid wait",
            )
        )
    return tuple(causes)


def reject_followup(inp: CheckInput) -> tuple[Cause, ...]:
    extra = timedelta(seconds=inp.config.times.reject_followup_extra_seconds)
    causes = []
    for card in _cards(inp):
        start = _open_reject(card)
        if start is None:
            continue
        deadline = start.at + timedelta(seconds=card.time_limit_seconds) + extra
        still_open = _running_since(card, start.at) or _review_requested_after(card, start.at)
        if inp.now < deadline or still_open:
            continue
        causes.append(
            _cause(
                "reject_followup",
                f"{card.id}:{start.id}",
                "reject did not reach a new review request",
                "Finish the new run and request review again",
            )
        )
    return tuple(causes)


def next_not_started(inp: CheckInput) -> tuple[Cause, ...]:
    cards = {card.id: card for card in _cards(inp)}
    limit = timedelta(seconds=inp.config.times.next_start_seconds)
    causes = []
    for card in cards.values():
        causes.extend(_next_causes(card, cards, inp, limit))
    return tuple(causes)


def intermediate_failure(inp: CheckInput) -> tuple[Cause, ...]:
    causes = []
    for card in _cards(inp):
        if card.gate or card.status not in FAILURE_STATUSES:
            continue
        causes.append(
            _cause(
                "intermediate_failure",
                f"{card.id}:{card.status}",
                f"intermediate card is {card.status}",
                "Wake the main agent for this intermediate card",
            )
        )
    return tuple(causes)


def unstarted_after_parents(inp: CheckInput) -> tuple[Cause, ...]:
    cards = {card.id: card for card in _cards(inp)}
    limit = timedelta(seconds=inp.config.times.unstarted_after_parents_seconds)
    causes = []
    for card in cards.values():
        cause = _unstarted(card, cards, inp.now, limit)
        if cause is not None:
            causes.append(cause)
    return tuple(causes)


def start_without_card(inp: CheckInput) -> tuple[Cause, ...]:
    cards = _cards(inp)
    by_key = {card.approval_key: card for card in cards if card.approval_key}
    by_id = {card.id: card for card in cards}
    links = {
        item.approval_key: item.card_id
        for item in inp.approvals
        if item.kind == "link" and item.approval_key and item.card_id
    }
    limit = timedelta(seconds=inp.config.times.start_approval_without_card_seconds)
    causes = []
    for item in inp.approvals:
        if item.kind != "start" or item.approval_key is None or item.approved_at is None:
            continue
        causes.extend(_start_causes(item, by_key, by_id, links, inp.now, limit))
    return tuple(causes)


def gate_subscription(inp: CheckInput) -> tuple[Cause, ...]:
    dest = inp.config.destinations.main_subscription
    causes = []
    for card in _cards(inp):
        if card.gate and dest not in card.subscriptions:
            causes.append(
                _cause(
                    "gate_subscription",
                    f"{card.id}:subscription",
                    "gate card has no main subscription",
                    "Restore the main subscription on this gate card",
                )
            )
    return tuple(causes)


def checklist_done(inp: CheckInput) -> tuple[Cause, ...]:
    causes = []
    for card in _cards(inp):
        if card.status != "done":
            continue
        problems = checklist_problems(card.checklist_path)
        if problems is None:
            causes.append(
                _cause(
                    "checklist_done",
                    f"{card.id}:checklist",
                    "done card has an unreadable checklist",
                    "Repair the checklist before treating the card as done",
                    certain=False,
                    uncheckable=True,
                )
            )
        elif problems:
            causes.append(
                _cause(
                    "checklist_done",
                    f"{card.id}:checklist",
                    "done card has a failing checklist item",
                    "Reopen the card or fix the failing checklist item",
                )
            )
    return tuple(causes)


def reject_cap(inp: CheckInput) -> tuple[Cause, ...]:
    cap = inp.config.limits.reject_cap
    causes = []
    for card in _cards(inp):
        if card.gate and card.status == "review" and card.reject_count >= cap:
            causes.append(
                _cause(
                    "reject_cap",
                    f"{card.id}:reject-cap",
                    "reject cap reached",
                    "Ask the user; do not open another reject",
                )
            )
    return tuple(causes)


def stalled(inp: CheckInput) -> tuple[Cause, ...]:
    limit = timedelta(seconds=inp.config.times.stall_unchanged_seconds)
    causes = []
    for card in _cards(inp):
        if card.status != "running":
            continue
        change = _last_change(card)
        if change == "unreadable":
            causes.append(
                _cause(
                    "stalled",
                    f"{card.id}:stall",
                    "progress or outputs are unreadable",
                    "Restore the progress file and outputs before the next scan",
                    certain=False,
                    uncheckable=True,
                )
            )
            continue
        started = _current_run_start(card)
        marks = [mark for mark in (change, started) if isinstance(mark, datetime)]
        if not marks or inp.now < max(marks) + limit:
            continue
        causes.append(
            _cause(
                "stalled",
                f"{card.id}:stall",
                "progress and outputs are unchanged",
                "Check the running card; its files have not changed",
            )
        )
    return tuple(causes)


def unchecked_result(inp: CheckInput) -> tuple[Cause, ...]:
    causes = []
    for card in _cards(inp):
        if card.status != "done":
            continue
        if not card.fingerprint or not card.basis:
            causes.append(
                _cause(
                    "unchecked_result",
                    f"{card.id}:result",
                    "done card has no fingerprint or basis",
                    "Record the result fingerprint and basis",
                    certain=False,
                    uncheckable=True,
                )
            )
        elif not _has_pass(inp.reviews, card):
            causes.append(
                _cause(
                    "unchecked_result",
                    f"{card.id}:result",
                    "done result has no PASS for its fingerprint and basis",
                    "Review this fingerprint and basis or reopen the card",
                )
            )
    return tuple(causes)


def applied_differs(inp: CheckInput) -> tuple[Cause, ...]:
    causes = []
    for card in _cards(inp):
        if card.applied_fingerprint is None:
            continue
        approved = {
            item.fingerprint
            for item in inp.approvals
            if item.kind == "apply" and item.card_id == card.id
        }
        if card.applied_fingerprint not in approved:
            causes.append(
                _cause(
                    "applied_differs",
                    f"{card.id}:applied",
                    "applied fingerprint is not the approved one",
                    "Stop and ask again; the applied result differs",
                )
            )
    return tuple(causes)


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


def _has_pass(reviews: Sequence[Review], card: Card) -> bool:
    # The latest round on this exact result and basis decides. An earlier PASS cannot
    # outvote a later FAIL, and a review that flagged a blocker is not a pass.
    same = [
        item
        for item in reviews
        if item.card == card.id
        and item.fingerprint == card.fingerprint
        and item.basis == card.basis
    ]
    if not same:
        return False
    latest = max(same, key=lambda item: item.round)
    return latest.verdict == "PASS" and not latest.blocking


def _apply_matches(item: Approval, card: Card) -> bool:
    return item.kind == "apply" and item.card_id == card.id and item.fingerprint == card.fingerprint


def _latest_work_event(card: Card) -> Event | None:
    events = [
        event
        for event in card.events
        if event.kind in WORK_KINDS
        and event.kind not in IGNORED_KINDS
        and event.id not in card.ignored_event_ids
    ]
    return max(events, key=lambda event: event.at) if events else None


def _acted(card: Card, event: Event, inp: CheckInput) -> bool:
    if card.status == "done" and card.done_at is not None and card.done_at >= event.at:
        return True
    if _review_reopened_after(card, event.at) and _run_after(card, event.at):
        return True
    return _wait_still_valid(card, event.id, inp)


def _wait_still_valid(card: Card, event_id: str, inp: CheckInput) -> bool:
    waits = [
        item
        for item in _handled(card, inp.config.comments)
        if item.event_id == event_id and item.action == "wait"
    ]
    if not waits:
        return False
    if card.status == "blocked":
        return True
    open_ids = set(inp.board.open_wait_ids())
    return any(item.argument in open_ids for item in waits)


def _handled(card: Card, rules: CommentRules) -> tuple[Handled, ...]:
    found = []
    for comment in card.comments:
        parsed = _parse_handled(comment, rules)
        if parsed is not None:
            found.append(parsed)
    return tuple(found)


def _parse_handled(comment: Comment, rules: CommentRules) -> Handled | None:
    text = comment.text.strip()
    marker = f" {rules.handled_prefix}"
    if text.startswith(rules.handled_prefix):
        rest = text[len(rules.handled_prefix) :]
    elif marker in text:
        rest = text.split(marker, 1)[1]
    else:
        return None
    event_id, separator, action = rest.partition(" action=")
    if not separator or not event_id or not action:
        return None
    if action == rules.approve:
        return Handled(event_id, "approve", "", comment.at)
    if action == rules.reject:
        return Handled(event_id, "reject", "", comment.at)
    if action.startswith(rules.wait_prefix):
        return Handled(event_id, "wait", action[len(rules.wait_prefix) :], comment.at)
    if action.startswith(rules.next_prefix):
        return Handled(event_id, "next", action[len(rules.next_prefix) :], comment.at)
    return None


def _open_reject(card: Card) -> Event | None:
    starts = [
        event
        for event in card.events
        if event.kind == "review_reopened"
        and event.id not in card.ignored_event_ids
        and _run_after(card, event.at)
        and not _review_requested_after(card, event.at)
    ]
    return max(starts, key=lambda event: event.at) if starts else None


def _review_reopened_after(card: Card, at: datetime) -> bool:
    return any(
        event.kind == "review_reopened"
        and event.at >= at
        and event.id not in card.ignored_event_ids
        for event in card.events
    )


def _review_requested_after(card: Card, at: datetime) -> bool:
    return any(
        event.kind == "review_requested"
        and event.at > at
        and event.id not in card.ignored_event_ids
        for event in card.events
    )


def _run_after(card: Card, at: datetime) -> bool:
    return any(run.started_at >= at for run in card.runs)


def _running_since(card: Card, at: datetime) -> bool:
    return any(run.status == "running" and run.started_at >= at for run in card.runs)


def _next_causes(
    card: Card,
    cards: Mapping[str, Card],
    inp: CheckInput,
    limit: timedelta,
) -> tuple[Cause, ...]:
    causes = []
    for item in _handled(card, inp.config.comments):
        if item.action != "next":
            continue
        started = _started_at(cards.get(item.argument), item.at)
        if started is not None and started <= item.at + limit:
            continue
        if started is None and inp.now < item.at + limit:
            continue
        causes.append(
            _cause(
                "next_not_started",
                f"{card.id}:{item.argument}",
                "next card did not start",
                f"Start card {item.argument}",
            )
        )
    return tuple(causes)


def _started_at(card: Card | None, at: datetime) -> datetime | None:
    if card is None:
        return None
    times = [run.started_at for run in card.runs if run.started_at >= at]
    return min(times) if times else None


def _unstarted(
    card: Card,
    cards: Mapping[str, Card],
    now: datetime,
    limit: timedelta,
) -> Cause | None:
    if card.runs or card.status not in {"todo", "ready"} or not card.parents:
        return None
    done_at = []
    for parent_id in card.parents:
        parent = cards.get(parent_id)
        if parent is None or (parent.status == "done" and parent.done_at is None):
            return _cause(
                "unstarted_after_parents",
                f"{card.id}:parents",
                "parent done time is unreadable",
                "Record when each parent finished",
                certain=False,
                uncheckable=True,
            )
        if parent.status != "done":
            return None
        done_at.append(parent.done_at)
    if now < max(done_at) + limit:
        return None
    return _cause(
        "unstarted_after_parents",
        f"{card.id}:unstarted",
        "parents are done and the card has not started",
        f"Start card {card.id}",
    )


def _start_causes(item, by_key, by_id, links, now, limit) -> tuple[Cause, ...]:
    key = item.approval_key
    card = by_key.get(key)
    link = links.get(key)
    aged = now >= item.approved_at + limit
    if link is not None and link not in by_id:
        return (
            _cause(
                "start_without_card",
                f"approval:{key}:missing-card",
                "linked card is missing",
                "Restore the linked card or record why it was removed",
            ),
        )
    if link is None and card is None and aged:
        return (
            _cause(
                "start_without_card",
                f"approval:{key}:no-card",
                "start approval has no card",
                "Create the card for this approval or drop the approval",
            ),
        )
    if link is None and card is not None and aged:
        return (
            _cause(
                "start_without_card",
                f"approval:{key}:no-link",
                "start approval has a card but no link",
                "Append the link line; do not create a second card",
            ),
        )
    return ()


def _last_change(card: Card) -> datetime | None | str:
    times = []
    for path in (card.progress_path, *card.output_paths):
        if not path:
            continue
        file = Path(path)
        try:
            if file.exists():
                times.append(datetime.fromtimestamp(file.stat().st_mtime, timezone.utc))
        except OSError:
            return "unreadable"
    return max(times) if times else None


def _current_run_start(card: Card) -> datetime | None:
    running = [run.started_at for run in card.runs if run.status == "running"]
    if running:
        return max(running)
    if card.status == "running" and card.runs:
        return max(run.started_at for run in card.runs)
    return None


def _warnings(warnings: Sequence[Warning], cause_id: str, channel: str) -> tuple[Warning, ...]:
    found = [item for item in warnings if item.cause_id == cause_id and item.channel == channel]
    return tuple(sorted(found, key=lambda item: item.warned_at))


def _spec(name: str, kind: str, blocks: str, removable_when: str, run) -> None:
    _register(CheckSpec(name, kind, blocks, removable_when, run))


_spec(
    "worker_finish",
    "gate",
    "completing without a zero exit, required outputs, and a passing checklist",
    "Hermes refuses those calls on the same evidence",
    worker_finish,
)
_spec(
    "pass_review",
    "gate",
    "approving a result with no PASS for the current fingerprint and basis",
    "Hermes stores the reviewed fingerprint and refuses a mismatch",
    pass_review,
)
_spec(
    "clean_result",
    "gate",
    "approving or applying a work copy that changed after the PASS",
    "Hermes rejects a dirty work copy on gate pass and apply",
    clean_result,
)
_spec(
    "apply_match",
    "gate",
    "applying a result other than the approved fingerprint",
    "the apply command compares the approved fingerprint itself",
    apply_match,
)
_spec(
    "gate_action",
    "watch",
    "a gate card with no real action after its latest review request",
    "Hermes records that action gap itself",
    gate_action,
)
_spec(
    "reject_followup",
    "watch",
    "a started reject that never reaches a new review request",
    "Hermes tracks reject completion itself",
    reject_followup,
)
_spec(
    "next_not_started",
    "watch",
    "a recorded next card that does not start",
    "Hermes starts that next card itself",
    next_not_started,
)
_spec(
    "intermediate_failure",
    "watch",
    "an intermediate card left in a failure status",
    "Hermes wakes the main agent for those statuses",
    intermediate_failure,
)
_spec(
    "unstarted_after_parents",
    "watch",
    "a card whose parents are done but which never starts",
    "Hermes promotes a ready child and alerts",
    unstarted_after_parents,
)
_spec(
    "start_without_card",
    "watch",
    "a start approval that never becomes a linked card",
    "card creation is transactional with the approval record",
    start_without_card,
)
_spec(
    "gate_subscription",
    "watch",
    "a gate card with no main subscription",
    "Hermes refuses to drop that subscription",
    gate_subscription,
)
_spec(
    "checklist_done",
    "watch",
    "a done card whose checklist has a failing item",
    "Hermes refuses done unless the checklist passes",
    checklist_done,
)
_spec(
    "reject_cap",
    "watch",
    "another automatic reject after the cap",
    "Hermes enforces the reject cap",
    reject_cap,
)
_spec(
    "stalled",
    "watch",
    "a running card whose progress and outputs are unchanged",
    "Hermes reports that card-level stall",
    stalled,
)
_spec(
    "unchecked_result",
    "watch",
    "a done result with no PASS for its fingerprint and basis",
    "the gate blocks done without that PASS",
    unchecked_result,
)
_spec(
    "applied_differs",
    "watch",
    "an applied fingerprint that is not in the approval record",
    "apply refuses a fingerprint mismatch before it writes",
    applied_differs,
)
