"""Live board adapter. Hermes is imported only when this module is called.

The library checks stay free of that import. Tests inject a fake board and
never reach these functions.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import textwrap
import time
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

    def open_wait_ids(self) -> tuple[str, ...]:
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
        self._owned: dict[str, str] = {}

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

    def open_wait_ids(self) -> tuple[str, ...]:
        # A handled comment is not a wait. The id has to be an open card.
        return tuple(item.id for item in self.cards() if item.status not in _CLOSED)

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
        card_id = self.create(title, body, blocked=True)
        self._owned[card_id] = "blocked"
        return card_id

    def subscribe(self, card_id: str, destination: str) -> None:
        try:
            spec = parse_destination(destination)
        except DestinationError as exc:
            raise OSError(str(exc)) from exc
        _, add_notify_sub = _notify()
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
                done = bool(_kanban().complete_task(conn, card_id, summary=summary))
        except Exception:
            return False
        if done:
            self._owned[card_id] = "done"
        return done

    def archive(self, card_id: str) -> None:
        with self._closing() as conn:
            if not _kanban().archive_task(conn, card_id):
                raise OSError(f"could not archive {card_id}")
        self._owned.pop(card_id, None)

    def statuses(self) -> dict[str, str]:
        if not self._owned:
            return {}
        found = {}
        with self._closing() as conn:
            kb = _kanban()
            for card_id, status in self._owned.items():
                task = kb.get_task(conn, card_id)
                found[card_id] = task.status if task is not None else status
        return found

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

    def reject_review(self, card_id: str, text: str) -> None:
        """Reopen the finished fix worker and everything behind it in one transaction.

        Only a Hermes whose reopen_review_task nests inside a transaction can do this.
        The caller checked that. Separate transactions would leave the old result done
        and let the reviewer run on it.
        """
        kb = _kanban()
        write_txn = _write_txn()
        terminations = []
        with self._closing() as conn:
            fixers = [
                pid for pid in kb.parent_ids(conn, card_id) if _status(kb, conn, pid) == "done"
            ]
            if not fixers:
                raise OSError(f"{card_id} has no finished fix worker to reopen")
            with write_txn(conn):
                for target in (card_id, *fixers):
                    kb.add_comment(conn, target, "taskwatch", text)
                if not kb.reopen_review_task(conn, card_id):
                    raise OSError(f"could not reopen {card_id}")
                for fixer in fixers:
                    _reopen_done(kb, conn, fixer)
                    result = kb.invalidate_descendants_for_parent_reopen(
                        conn, fixer, author="taskwatch"
                    )
                    terminations.extend(result["terminations"])
        # Hermes kills a running descendant's worker only after the transaction committed.
        for pid, claim_lock, started_at in terminations:
            kb._terminate_reclaimed_worker(pid, claim_lock, started_at=started_at)

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
        _, _add = _notify()
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

    return Probe(_hermes_version(), nested_reopen_available(), _enabled(root))


def _enabled(root: Path):
    return lambda profile: _plugin_enabled(root, profile)


def nested_reopen_available() -> bool | None:
    """True when installed reopen_review_task calls write_txn(allow_nested=True)."""
    try:
        fn = getattr(_kanban(), "reopen_review_task", None)
        if fn is None:
            return None
        source = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(source)
    except Exception:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) != "write_txn":
            continue
        for keyword in node.keywords:
            if keyword.arg != "allow_nested":
                continue
            value = keyword.value
            if isinstance(value, ast.Constant) and value.value is True:
                return True
    return False


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


def db_path(slug: str | None, *, honor_env: bool) -> str:
    """Path Hermes would open. The configured board ignores a worker pin."""
    from hermes_cli.kanban_db import kanban_db_path

    if honor_env:
        return str(kanban_db_path(slug))
    saved = os.environ.pop("HERMES_KANBAN_DB", None)
    try:
        return str(kanban_db_path(slug))
    finally:
        if saved is not None:
            os.environ["HERMES_KANBAN_DB"] = saved


def _kanban():
    from hermes_cli import kanban_db

    return kanban_db


def _connect_closing():
    from hermes_cli.kanban_db_connect import connect_closing

    return connect_closing


def _write_txn():
    from hermes_cli.kanban_db_connect import write_txn

    return write_txn


def _notify():
    from hermes_cli.kanban_db_notify import add_notify_sub

    return None, add_notify_sub


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


def _plugin_profile() -> str | None:
    from .plugin import plugin_profile

    return plugin_profile()


def _active_profile() -> str | None:
    try:
        from hermes_cli.profiles import get_active_profile_name
    except Exception:
        return None
    try:
        return get_active_profile_name()
    except Exception:
        return None


def _status(kb, conn, card_id: str) -> str | None:
    task = kb.get_task(conn, card_id)
    return task.status if task is not None else None


def _reopen_done(kb, conn, card_id: str) -> None:
    """done -> ready, or todo while a parent is still open. Hermes has no function for this;
    its dashboard writes the same row and event. Call it inside the caller's transaction."""
    parents = [_status(kb, conn, parent) for parent in kb.parent_ids(conn, card_id)]
    landing = "ready" if all(item in _CLOSED for item in parents) else "todo"
    cur = conn.execute(
        "UPDATE tasks SET status = ?, completed_at = NULL, current_run_id = NULL, "
        "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
        "WHERE id = ? AND status = 'done'",
        (landing, card_id),
    )
    if cur.rowcount != 1:
        raise OSError(f"could not reopen {card_id}")
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, NULL, 'status', ?, ?)",
        (card_id, json.dumps({"status": landing, "reason": "taskwatch reject"}), int(time.time())),
    )


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


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""
