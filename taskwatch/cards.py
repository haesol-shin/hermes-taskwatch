"""Create a card from intent and spec, or reject a gate.

taskwatch does not start the next chain. A review-gate reject needs a Hermes
that can nest reopen_review_task. Without that, the reject is refused.

Both tools change the board and the state files in several steps, so each runs
under the state lock from the first lookup to the last write.
"""

from datetime import datetime
from pathlib import Path

from .card_body import dumps, loads
from .config import Config
from .evidence import REJECTS, reject_counts
from .state import RecordError, State, StateError

_INTENT = ("Source", "Purpose", "Done", "Approval")
_SPEC = ("Outputs", "Checklist", "Out of scope", "Live", "Guidance", "Model")
_KINDS = ("worker", "review")


class CardError(ValueError):
    """The tool refused. No running card was left behind."""


def create_cards(
    config: Config,
    state: State,
    writer,
    *,
    approval_key: str,
    intent: Path,
    spec: Path,
    cards: tuple[dict, ...],
    now: datetime,
) -> dict:
    missing = _missing(intent, _INTENT) + _missing(spec, _SPEC)
    if missing:
        return {"ok": False, "error": f"missing {missing[0]}"}
    problem = _card_problem(cards)
    if problem:
        return {"ok": False, "error": problem}
    # Two calls for one approval must not both pass the lookup and create a card.
    with state.lock():
        return _create_locked(config, state, writer, approval_key, intent, spec, cards, now)


def _create_locked(config, state, writer, approval_key, intent, spec, cards, now) -> dict:
    try:
        records = state.load()
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    if records.error:
        return {"ok": False, "error": records.error}
    starts = [
        item
        for item in records.approvals
        if item.kind == "start" and item.approval_key == approval_key
    ]
    if not starts:
        return {"ok": False, "error": "start approval missing"}
    links = [
        item.card_id
        for item in records.approvals
        if item.kind == "link" and item.approval_key == approval_key
    ]
    if links:
        return {"ok": True, "card_id": links[0], "recovered": False, "duplicate": True}
    existing = writer.find_approval(approval_key)
    if existing:
        state.append_link(approval_key, existing, now)
        return {"ok": True, "card_id": existing, "recovered": True}
    created, error = _create_all(config, writer, cards, approval_key, intent, spec)
    if created is None:
        return {"ok": False, "error": error}
    try:
        state.append_link(approval_key, created[0], now)
    except StateError as exc:
        return {"ok": False, "error": str(exc), "card_id": created[0]}
    return {"ok": True, "card_id": created[0], "recovered": False}


def reject_gate(
    config: Config,
    state: State,
    writer,
    *,
    card_id: str,
    reason: str,
    reject_id: str,
    now: datetime,
    nested_reopen: bool,
) -> dict:
    """Open one more round on a gate card, once per reject id.

    The id is written before the board changes, with the card's last event id at that
    moment. A retry with the same id finds it and finishes the step that did not happen.
    The board's own reopen event after that mark decides whether the reopen already
    happened. The cap counts finished rejects. One unfinished reject can exist at a time;
    another id is refused until the pending one is retried.
    """
    if not reason.strip() or not reject_id.strip():
        return {"ok": False, "error": "reason and reject id are required"}
    with state.lock():
        try:
            records = state.read_records(REJECTS)
        except (RecordError, OSError) as exc:
            return {"ok": False, "error": str(exc)}
        mine = [item for item in records if item.get("reject_id") == reject_id]
        if any(item.get("card_id") != card_id for item in mine):
            return {"ok": False, "error": "reject id belongs to another card"}
        for item in mine:
            if item.get("phase") == "done":
                return item["result"]
        pending = _pending(records, card_id)
        if pending and pending != reject_id:
            return {"ok": False, "error": "another reject is pending", "pending": pending}
        refused = _refusal(writer, card_id, nested_reopen, started=bool(mine))
        if refused:
            return refused
        if mine:
            round_, mark = mine[0]["round"], mine[0].get("event_mark")
            if not isinstance(mark, int):
                return {"ok": False, "error": "the reject record has no event mark"}
        else:
            done = reject_counts(state).get(card_id, 0)
            if done >= config.limits.reject_cap:
                return {"ok": False, "error": "reject cap", "opened": False}
            round_ = done + 1
            try:
                mark = writer.event_mark(card_id)
            except OSError as exc:
                return {"ok": False, "error": str(exc), "opened": False, "retry": True}
            _store(state, card_id, reject_id, "started", round_, now, mark=mark)
        return _open(state, writer, card_id, reject_id, reason, round_, mark, now)


def _pending(records: tuple[dict, ...], card_id: str) -> str | None:
    """The id of a reject that started on this card and has not finished."""
    finished = {str(item.get("reject_id")) for item in records if item.get("phase") == "done"}
    for item in records:
        started = item.get("phase") == "started" and item.get("card_id") == card_id
        if started and str(item.get("reject_id")) not in finished:
            return str(item["reject_id"])
    return None


def _refusal(writer, card_id: str, nested_reopen: bool, *, started: bool) -> dict | None:
    role = writer.gate_role(card_id)
    waiting = writer.waiting(card_id)
    if role == "unknown":
        return {"ok": False, "error": "unknown gate role", "opened": False}
    if role == "review" and waiting and not nested_reopen:
        return {"ok": False, "error": "core update needed", "opened": False}
    if not waiting and not started:
        return {"ok": False, "error": "card is not waiting for a decision", "opened": False}
    return None


def _open(state, writer, card_id, reject_id, reason, round_, mark, now) -> dict:
    text = f"reject {reject_id}: {reason}"
    try:
        # An earlier try of this id may have reopened the card and failed before it wrote
        # "done". The card can be waiting again by now, so the board's own reopen event
        # after the mark decides, not the status.
        if writer.waiting(card_id) and not writer.reopened_after(card_id, mark):
            if writer.gate_role(card_id) == "review":
                writer.reject_review(card_id, text)
            else:
                writer.reject_worker(card_id, text)
    except OSError as exc:
        return {"ok": False, "error": str(exc), "opened": False, "retry": True}
    result = {"ok": True, "opened": True, "round": round_, "reject_id": reject_id}
    _store(state, card_id, reject_id, "done", round_, now, result)
    return result


def _store(state, card_id, reject_id, phase, round_, now, result=None, mark=None) -> None:
    payload = {
        "card_id": card_id,
        "reject_id": reject_id,
        "phase": phase,
        "round": round_,
        "at": now.isoformat(),
    }
    if mark is not None:
        payload["event_mark"] = mark
    if result is not None:
        payload["result"] = result
    state.append_record(REJECTS, payload, now)


def _card_problem(cards: tuple[dict, ...]) -> str | None:
    if not cards:
        return "no cards to create"
    for index, card in enumerate(cards):
        if not isinstance(card.get("title"), str) or not card["title"].strip():
            return f"card {index} needs a title"
        kind = card.get("kind", "worker")
        if kind not in _KINDS or (kind == "review" and not card.get("gate")):
            return f"card {index} has a kind that is not worker or a gate review"
        for parent in card.get("parents", ()):
            if isinstance(parent, bool) or not isinstance(parent, int) or not 0 <= parent < index:
                return f"card {index} names a parent that is not an earlier card"
    return None


def _missing(path: Path, names: tuple[str, ...]) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [path.name]
    found = []
    for name in names:
        heading = f"## {name}\n"
        if heading not in text:
            found.append(name)
            continue
        body = text.split(heading, 1)[1].split("\n## ", 1)[0].strip()
        if not body:
            found.append(name)
    return found


def _create_all(config, writer, cards, approval_key, intent, spec):
    made: list[str] = []
    try:
        for index, card in enumerate(cards):
            parents = tuple(made[parent] for parent in card.get("parents", ()))
            body = dumps(
                {
                    "gate": bool(card.get("gate")),
                    "kind": card.get("kind", "worker"),
                    "approval_key": approval_key,
                    "intent_path": str(intent),
                    "spec_path": str(spec),
                }
            )
            made.append(writer.create(card["title"], body, parents=parents, blocked=index == 0))
        for card_id, card in zip(made, cards, strict=True):
            if not card.get("gate"):
                continue
            writer.subscribe(card_id, config.destinations.main_subscription)
            if not writer.subscribed(card_id, config.destinations.main_subscription):
                raise CardError("subscription missing")
        writer.unblock(made[0])
    except (CardError, OSError) as exc:
        stuck = _roll_back(writer, made)
        detail = f"subscription failed: {exc}"
        return None, detail if stuck is None else f"{detail}; rollback stopped at {stuck}"
    return made, None


def _roll_back(writer, made: list[str]) -> str | None:
    """Archive children before their parents. Hermes promotes a child when its parent is archived,
    so a parent archived first would let the child run while the rollback is still going."""
    for card_id in reversed(made):
        try:
            writer.archive(card_id)
        except OSError:
            return card_id
    return None


def approval_key_of(body: str | None) -> str | None:
    fields = loads(body)
    key = fields.get("approval_key")
    return key if isinstance(key, str) else None
