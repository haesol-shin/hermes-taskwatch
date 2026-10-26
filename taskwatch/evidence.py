"""Card evidence computed from the result itself, never from fields the card carries.

run_executor writes one line per run to state/runs.jsonl: where the result lives and
how the run ended. The gate and the scan read that line, hash the result as it is now,
and fill the card. A recorded review counts only when it matches this live value.
taskwatch_reject writes state/rejects.jsonl; a card's reject count comes from it.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from .fingerprint import (
    FileResult,
    FingerprintError,
    GitResult,
    basis_fingerprint,
    files_fingerprint,
    head_sha,
    uncommitted_changes,
)
from .protocol import BoardReader, BoardReadError, Card
from .state import RecordError, State

RUNS = "runs.jsonl"
REJECTS = "rejects.jsonl"


@dataclass(frozen=True, slots=True)
class Run:
    run_id: str
    card: str
    kind: str
    exit_code: int
    copy: str
    git: bool
    results: tuple[str, ...]
    checklist: str | None = None
    progress: str | None = None
    guidelines: tuple[str, ...] = ()
    log: str | None = None


def append_run(state: State, run: Run, now: datetime) -> None:
    state.append_record(
        RUNS,
        {
            "run_id": run.run_id,
            "card": run.card,
            "kind": run.kind,
            "exit_code": run.exit_code,
            "copy": run.copy,
            "git": run.git,
            "results": list(run.results),
            "checklist": run.checklist,
            "progress": run.progress,
            "guidelines": list(run.guidelines),
            "log": run.log,
            "at": now.isoformat(),
        },
        now,
    )


def load_runs(state: State) -> tuple[Run, ...]:
    return tuple(_run(obj, n) for n, obj in enumerate(state.read_records(RUNS), 1))


def reject_counts(state: State) -> dict[str, int]:
    """Finished rejects per gate card. A started reject that did not finish is not counted."""
    done: dict[str, set[str]] = {}
    for obj in state.read_records(REJECTS):
        if obj.get("phase") == "done":
            done.setdefault(str(obj.get("card_id")), set()).add(str(obj.get("reject_id")))
    return {card: len(ids) for card, ids in done.items()}


def basis_of(intent: Path, spec: Path, guidelines: Sequence[Path]) -> str:
    """The review basis. The reviewer and the gate must name the files the same way."""
    named = [("intent", intent), ("spec", spec)]
    named += [(path.name, path) for path in guidelines]
    return basis_fingerprint(named)


def result_fingerprint(copy: Path, git: bool, results: Sequence[str]) -> tuple[str, bool]:
    """Fingerprint of the result as it is now, and whether a Git copy has uncommitted changes."""
    if git:
        return head_sha(copy), bool(uncommitted_changes(copy))
    return files_fingerprint(copy, results), False


class EvidenceBoard:
    """BoardReader that fills the evidence fields of a card from records and the live result."""

    def __init__(self, board: BoardReader, state: State) -> None:
        self.board = board
        self.state = state

    def cards(self) -> tuple[Card, ...]:
        runs, counts = self._records()
        return tuple(_fill(card, runs, counts) for card in self.board.cards())

    def card(self, card_id: str) -> Card | None:
        card = self.board.card(card_id)
        if card is None:
            return None
        runs, counts = self._records()
        return _fill(card, runs, counts)

    def open_wait_ids(self, cards: tuple[Card, ...] | None = None) -> tuple[str, ...]:
        return self.board.open_wait_ids(cards)

    def read_error(self) -> str | None:
        try:
            self._records()
        except BoardReadError as exc:
            return str(exc)
        return self.board.read_error()

    def result(self, card: Card) -> GitResult | FileResult | None:
        """What to freeze: the Git commit that was checked, or the files that were hashed."""
        run = _own_run(tuple(item for item in load_runs(self.state) if item.card == card.id))
        if run is None or card.fingerprint is None:
            return None
        if run.git:
            return GitResult(Path(run.copy), card.fingerprint)
        return FileResult(Path(run.copy), run.results)

    def _records(self) -> tuple[tuple[Run, ...], dict[str, int]]:
        try:
            return load_runs(self.state), reject_counts(self.state)
        except (RecordError, OSError) as exc:
            raise BoardReadError(str(exc)) from exc


def _fill(card: Card, runs: tuple[Run, ...], counts: dict[str, int]) -> Card:
    card = replace(
        card,
        reject_count=counts.get(card.id, 0),
        exit_code=None,
        required_outputs=(),
        output_paths=(),
        checklist_path=None,
        progress_path=None,
        fingerprint=None,
        basis=None,
        dirty=False,
    )
    mine = tuple(run for run in runs if run.card == card.id)
    run = _own_run(mine)
    if run is None:
        return card
    reviews = [item for item in mine if item.kind == "review"]
    guidelines = reviews[-1].guidelines if reviews else ()
    fingerprint, dirty = _live_result(run)
    paths = tuple(str(Path(run.copy) / item) for item in run.results)
    return replace(
        card,
        exit_code=run.exit_code,
        required_outputs=paths,
        output_paths=paths,
        checklist_path=run.checklist,
        progress_path=run.progress,
        fingerprint=fingerprint,
        dirty=dirty,
        basis=_live_basis(card, guidelines),
    )


def _own_run(mine: tuple[Run, ...]) -> Run | None:
    """The card's latest work run. A review card has none, so its latest review run."""
    for kind in ("work", "review"):
        found = [run for run in mine if run.kind == kind]
        if found:
            return found[-1]
    return None


def _live_result(run: Run) -> tuple[str | None, bool]:
    try:
        return result_fingerprint(Path(run.copy), run.git, run.results)
    except (FingerprintError, OSError):
        # No fingerprint means no review can match it. A Git copy that cannot be read is dirty.
        return None, run.git


def _live_basis(card: Card, guidelines: tuple[str, ...]) -> str | None:
    if not card.intent_path or not card.spec_path:
        return None
    try:
        return basis_of(
            Path(card.intent_path), Path(card.spec_path), tuple(Path(item) for item in guidelines)
        )
    except (FingerprintError, OSError):
        return None


def _run(obj: dict, number: int) -> Run:
    where = f"{RUNS}:{number}"
    try:
        return Run(
            run_id=_str(obj, "run_id", where),
            card=_str(obj, "card", where),
            kind=_kind(obj, where),
            exit_code=_int(obj, where),
            copy=_str(obj, "copy", where),
            git=_bool(obj, "git", where),
            results=_strs(obj, "results", where),
            checklist=_opt(obj, "checklist", where),
            progress=_opt(obj, "progress", where),
            guidelines=_strs(obj, "guidelines", where),
            log=_opt(obj, "log", where),
        )
    except KeyError as exc:
        raise RecordError(f"{where}: missing {exc.args[0]}") from exc


def _str(obj: dict, key: str, where: str) -> str:
    value = obj[key]
    if not isinstance(value, str) or not value:
        raise RecordError(f"{where}: bad {key}")
    return value


def _opt(obj: dict, key: str, where: str) -> str | None:
    value = obj.get(key)
    if value is not None and not isinstance(value, str):
        raise RecordError(f"{where}: bad {key}")
    return value


def _strs(obj: dict, key: str, where: str) -> tuple[str, ...]:
    value = obj[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RecordError(f"{where}: bad {key}")
    return tuple(value)


def _bool(obj: dict, key: str, where: str) -> bool:
    value = obj[key]
    if not isinstance(value, bool):
        raise RecordError(f"{where}: bad {key}")
    return value


def _int(obj: dict, where: str) -> int:
    value = obj["exit_code"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecordError(f"{where}: bad exit_code")
    return value


def _kind(obj: dict, where: str) -> str:
    value = obj["kind"]
    if value not in {"work", "review"}:
        raise RecordError(f"{where}: bad kind")
    return value
