"""Live board adapter. Hermes is imported only when this module is called.

The library checks stay free of that import. Tests inject a fake board and
never reach these functions.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .card_body import loads, replace_fields
from .destinations import DestinationError, parse_destination
from .protocol import BoardReadError, Card
from .protocol import Comment as TwComment
from .protocol import Event as TwEvent
from .protocol import Run as TwRun
from .units import profile_home

PLUGIN_NAME = "hermes-taskwatch"
_CLOSED = frozenset({"done", "archived"})


class UnavailableBoard:
    """Hermes could not be imported. A check must not treat that as a clean board."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def cards(self) -> tuple[Card, ...]:
        raise BoardReadError(self.reason)

    def card(self, card_id: str) -> Card | None:
        del card_id
        raise BoardReadError(self.reason)

    def open_wait_ids(self, cards: tuple[Card, ...] | None = None) -> tuple[str, ...]:
        del cards
        raise BoardReadError(self.reason)

    def read_error(self) -> str | None:
        return self.reason

    def record_snapshot(self, card_id: str, path: str) -> None:
        del card_id, path
        raise OSError(self.reason)


class HermesBoard:
    """BoardReader and card writer over Hermes Kanban functions."""

    def __init__(self, slug: str | None = None, db_path: str | None = None) -> None:
        self.slug = slug
        self.db_path = db_path

    def cards(self) -> tuple[Card, ...]:
        try:
            kb = _kanban()
            with self._closing() as conn:
                tasks = kb.list_tasks(conn)
                return tuple(self._one(kb, conn, task) for task in tasks)
        except BoardReadError:
            raise
        except Exception as exc:
            raise BoardReadError(str(exc)) from exc

    def card(self, card_id: str) -> Card | None:
        try:
            kb = _kanban()
            with self._closing() as conn:
                task = kb.get_task(conn, card_id)
                if task is None or task.status == "archived":
                    return None
                return self._one(kb, conn, task)
        except BoardReadError:
            raise
        except Exception as exc:
            raise BoardReadError(str(exc)) from exc

    def open_wait_ids(self, cards: tuple[Card, ...] | None = None) -> tuple[str, ...]:
        # A handled comment is not a wait. The id has to be an open card.
        cards = self.cards() if cards is None else cards
        return tuple(item.id for item in cards if item.status not in _CLOSED)

    def read_error(self) -> str | None:
        try:
            with self._closing() as conn:
                _kanban().list_tasks(conn, limit=1)
        except Exception as exc:
            return str(exc)
        return None

    def create(
        self, title: str, body: str, parents: tuple[str, ...] = (), blocked: bool = False
    ) -> str:
        # "running" is only a valid initial token. Hermes lands a parentless
        # card on ready and a child whose parents are open on todo.
        initial = "blocked" if blocked else "running"
        with self._closing() as conn:
            return _kanban().create_task(
                conn,
                title=title,
                body=body,
                parents=parents,
                initial_status=initial,
                workspace_kind="scratch",
            )

    def create_blocked(self, title: str, body: str) -> str:
        return self.create(title, body, blocked=True)

    def subscribe(self, card_id: str, destination: str) -> None:
        try:
            spec = parse_destination(destination)
        except DestinationError as exc:
            raise OSError(str(exc)) from exc
        add_notify_sub = _notify()
        with self._closing() as conn:
            add_notify_sub(
                conn,
                task_id=card_id,
                platform=spec["platform"],
                chat_id=spec["chat_id"],
                thread_id=spec.get("thread_id"),
                chat_type=spec.get("chat_type"),
                notifier_profile=spec.get("profile"),
                delivery_mode=spec.get("delivery_mode"),
                delivery_metadata={"taskwatch_destination": destination},
            )

    def subscribed(self, card_id: str, destination: str) -> bool:
        return destination in self._subscription_strings(card_id)

    def complete(self, card_id: str, summary: str) -> bool:
        try:
            with self._closing() as conn:
                return bool(_kanban().complete_task(conn, card_id, summary=summary))
        except Exception:
            return False

    def archive(self, card_id: str) -> None:
        with self._closing() as conn:
            if not _kanban().archive_task(conn, card_id):
                raise OSError(f"could not archive {card_id}")

    def unblock(self, card_id: str) -> None:
        with self._closing() as conn:
            if not _kanban().unblock_task(conn, card_id):
                raise OSError(f"could not unblock {card_id}")

    def find_approval(self, key: str) -> str | None:
        with self._closing() as conn:
            for task in _kanban().list_tasks(conn):
                if loads(task.body).get("approval_key") == key:
                    return task.id
        return None

    def gate_role(self, card_id: str) -> str:
        """The gate's role comes from its kind, which creation wrote. Its status says nothing."""
        with self._closing() as conn:
            task = _kanban().get_task(conn, card_id)
        fields = loads(task.body) if task is not None else {}
        if not fields.get("gate"):
            return "unknown"
        return "review" if fields.get("kind") == "review" else "worker"

    def waiting(self, card_id: str) -> bool:
        with self._closing() as conn:
            task = _kanban().get_task(conn, card_id)
        return task is not None and task.status == "review"

    def event_mark(self, card_id: str) -> int:
        """The id of the card's newest event, or 0. A reject records it when it starts."""
        with self._closing() as conn:
            events = _kanban().list_events(conn, card_id)
        return max((item.id for item in events), default=0)

    def reopened_after(self, card_id: str, mark: int) -> bool:
        """True when the card was reopened from review by an event newer than ``mark``."""
        with self._closing() as conn:
            events = _kanban().list_events(conn, card_id)
        return any(item.kind == "review_reopened" and item.id > mark for item in events)

    def reject_worker(self, card_id: str, text: str) -> None:
        """Comment, then send the worker back. The comment names the reject, so a retry skips it."""
        kb = _kanban()
        with self._closing() as conn:
            if not any(item.body == text for item in kb.list_comments(conn, card_id)):
                kb.add_comment(conn, card_id, "taskwatch", text)
            if not kb.reopen_review_task(conn, card_id):
                raise OSError(f"could not reopen {card_id}")

    def record_snapshot(self, card_id: str, path: str) -> None:
        kb = _kanban()
        with self._closing() as conn:
            task = kb.get_task(conn, card_id)
            if task is None:
                raise OSError(f"unknown task {card_id}")
            body = replace_fields(task.body, {"snapshot_path": path})
            if not kb.edit_task(conn, card_id, body=body):
                raise OSError(f"could not record snapshot on {card_id}")

    def _closing(self):
        kb = _kanban()
        connect_closing = _connect_closing()
        path = Path(self.db_path) if self.db_path else kb.kanban_db_path(self.slug)
        return connect_closing(path)

    def _one(self, kb, conn, task) -> Card:
        fields = loads(task.body)
        done_at = None
        if task.status == "done" and task.completed_at:
            done_at = _at(task.completed_at)
        return Card(
            id=task.id,
            status=task.status,
            gate=bool(fields.get("gate")),
            parents=tuple(kb.parent_ids(conn, task.id)),
            done_at=done_at,
            events=tuple(
                TwEvent(str(item.id), item.kind, _at(item.created_at))
                for item in kb.list_events(conn, task.id)
            ),
            comments=tuple(
                TwComment(item.body, _at(item.created_at))
                for item in kb.list_comments(conn, task.id)
            ),
            runs=tuple(
                TwRun(str(item.id), _at(item.started_at), item.status)
                for item in kb.list_runs(conn, task.id)
            ),
            subscriptions=self._subscription_strings(task.id, conn),
            ignored_event_ids=_texts(fields, "ignored_event_ids"),
            time_limit_seconds=_int(fields.get("time_limit_seconds"), 0),
            approval_key=_text(fields, "approval_key"),
            applied_fingerprint=_text(fields, "applied_fingerprint"),
            intent_path=_text(fields, "intent_path"),
            spec_path=_text(fields, "spec_path"),
            snapshot_path=_text(fields, "snapshot_path"),
            warning_for=_text(fields, "warning_for"),
        )

    def _subscription_strings(self, card_id: str, conn=None) -> tuple[str, ...]:
        if conn is None:
            with self._closing() as owned:
                return self._subscription_strings(card_id, owned)
        list_notify_subs = _list_notify_subs()
        found = []
        for item in list_notify_subs(conn, card_id):
            meta = item.get("delivery_metadata") or {}
            exact = meta.get("taskwatch_destination")
            if isinstance(exact, str) and exact:
                found.append(exact)
        return tuple(found)


def open_board(slug: str | None = None, db_path: str | None = None):
    try:
        _kanban()
    except Exception as exc:
        return UnavailableBoard(f"Hermes is not importable: {exc}")
    return HermesBoard(slug, db_path)


def live_probe(root: Path):
    from .commands import Probe

    return Probe(_hermes_version(), _enabled(root))


def _enabled(root: Path):
    return lambda profile: _plugin_enabled(root, profile)


def deliver_user(destination: str, text: str) -> None:
    """Send one message to the user destination with Hermes' send path (what `hermes send` uses).

    It needs no gateway session. A refusal or a platform error raises OSError, so the caller
    does not record a delivery that did not happen.
    """
    try:
        spec = parse_destination(destination)
    except DestinationError as exc:
        raise OSError(str(exc)) from exc
    target = ":".join(
        part for part in (spec["platform"], spec["chat_id"], spec.get("thread_id")) if part
    )
    from tools.send_message_tool import send_message_tool

    try:
        result = json.loads(
            send_message_tool({"action": "send", "target": target, "message": text})
        )
    except (ValueError, TypeError) as exc:
        raise OSError(f"unreadable send result: {exc}") from exc
    if not isinstance(result, dict) or result.get("error") or not result.get("success"):
        detail = result.get("error") if isinstance(result, dict) else None
        raise OSError(str(detail or "send was not confirmed"))


def hermes_root() -> Path:
    # The shared root has no public API; get_hermes_home() is the active profile home.
    from hermes_constants import get_default_hermes_root

    return Path(get_default_hermes_root())


def db_path(slug: str | None) -> str:
    """Path the next Hermes call opens: a pinned HERMES_KANBAN_DB wins over the name."""
    from hermes_cli.kanban_db import kanban_db_path

    return str(kanban_db_path(slug))


def configured_db_path(slug: str) -> str:
    """Path of the configured board's file, ignoring a pin. Same layout as kanban_db_path."""
    from hermes_cli.kanban_db import board_dir, kanban_home

    slug = slug.strip().lower()
    base = kanban_home() if slug == "default" else board_dir(slug)
    return str(base / "kanban.db")


def _kanban():
    from hermes_cli import kanban_db

    return kanban_db


def _connect_closing():
    from hermes_cli.kanban_db_connect import connect_closing

    return connect_closing


def _notify():
    from hermes_cli.kanban_db_notify import add_notify_sub

    return add_notify_sub


def _list_notify_subs():
    from hermes_cli.kanban_db_notify import list_notify_subs

    return list_notify_subs


def _hermes_version() -> str | None:
    try:
        from hermes_cli import __version__
    except Exception:
        return None
    return __version__ if isinstance(__version__, str) else None


def _plugin_enabled(root: Path, profile: str) -> bool | None:
    try:
        from hermes_cli.config import read_user_config_raw
    except Exception:
        return None
    path = Path(profile_home(str(root), profile)) / "config.yaml"
    if not path.is_file():
        return False
    try:
        raw = read_user_config_raw(path)
    except Exception:
        return None
    plugins = raw.get("plugins") if isinstance(raw, dict) else None
    if not isinstance(plugins, dict):
        return False
    enabled = plugins.get("enabled")
    disabled = plugins.get("disabled")
    if enabled is not None and not isinstance(enabled, list):
        return None
    if disabled is not None and not isinstance(disabled, list):
        return None
    names = {PLUGIN_NAME}
    if names & set(disabled or []):
        return False
    if not isinstance(enabled, list):
        return False
    return bool(names & set(enabled))


def _at(epoch: int) -> datetime:
    return datetime.fromtimestamp(int(epoch), timezone.utc)


def _text(fields: dict, key: str) -> str | None:
    value = fields.get(key)
    return value if isinstance(value, str) and value else None


def _texts(fields: dict, key: str) -> tuple[str, ...]:
    value = fields.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _int(value: object, default: int) -> int:
    parsed = _optional_int(value)
    return default if parsed is None else parsed
