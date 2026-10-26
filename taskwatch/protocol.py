"""Board access is the only protocol, so tests can use a fake board."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class BoardReadError(Exception):
    """The board could not be read. A check must not treat that as success."""


@dataclass(frozen=True, slots=True)
class Event:
    id: str
    kind: str
    at: datetime


@dataclass(frozen=True, slots=True)
class Comment:
    text: str
    at: datetime


@dataclass(frozen=True)
class Run:
    id: str
    started_at: datetime
    status: str


@dataclass(frozen=True)
class Card:
    id: str
    status: str
    gate: bool = False
    parents: tuple[str, ...] = ()
    done_at: datetime | None = None
    events: tuple[Event, ...] = ()
    comments: tuple[Comment, ...] = ()
    runs: tuple[Run, ...] = ()
    subscriptions: tuple[str, ...] = ()
    ignored_event_ids: tuple[str, ...] = ()
    time_limit_seconds: int = 0
    reject_count: int = 0
    approval_key: str | None = None
    exit_code: int | None = None
    required_outputs: tuple[str, ...] = ()
    checklist_path: str | None = None
    output_paths: tuple[str, ...] = ()
    progress_path: str | None = None
    fingerprint: str | None = None
    basis: str | None = None
    dirty: bool = False
    applied_fingerprint: str | None = None
    intent_path: str | None = None
    spec_path: str | None = None
    snapshot_path: str | None = None
    # Set on a card taskwatch wrote itself. The value is the cause it warns about.
    warning_for: str | None = None


class BoardReader(Protocol):
    def cards(self) -> tuple[Card, ...]:
        """Return the cards currently on the board."""

    def card(self, card_id: str) -> Card | None:
        """Return one card, or None if it is not on the board."""

    def open_wait_ids(self) -> tuple[str, ...]:
        """Return wait ids that are still open. A comment alone is not enough."""

    def read_error(self) -> str | None:
        """Return why the board cannot be read, or None when the read succeeded."""
