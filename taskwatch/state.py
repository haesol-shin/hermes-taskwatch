"""Append-only state under the Hermes root.

Lines are not rewritten. A torn tail is kept beside the file; a bad committed
line makes the file unreadable so a check cannot pass.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

RECORD_VERSION = 1
# A body file is named from a card id, so both share one first-character rule.
_NAME = r"[A-Za-z0-9][A-Za-z0-9._:-]*"
_ID = re.compile(rf"^{_NAME}$")
_START_KEYS = frozenset({"version", "kind", "approval_key", "approved_at", "approved"})
_LINK_KEYS = frozenset({"version", "kind", "approval_key", "card_id"})
_APPLY_KEYS = frozenset({"version", "kind", "approved", "message_id", "card_id", "fingerprint"})
_REVIEW_KEYS = frozenset(
    {
        "version",
        "round",
        "card",
        "fingerprint",
        "basis",
        "reviewer",
        "verdict",
        "blocking",
        "body_file",
        "body_sha256",
    }
)
_BODY = re.compile(rf"^{_NAME}\.md$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_WARNING_KEYS = frozenset({"version", "cause_id", "warned_at", "channel", "summary"})


class StateError(ValueError):
    """A write was refused. The file was not changed."""


class RecordError(ValueError):
    """A committed line cannot be read. Checks must not pass."""


@dataclass(frozen=True, slots=True)
class Approval:
    version: int
    kind: str
    approval_key: str | None
    approved_at: datetime | None = None
    approved: str | None = None
    card_id: str | None = None
    message_id: str | None = None
    fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class Review:
    version: int
    round: int
    card: str
    fingerprint: str
    basis: str
    reviewer: str
    verdict: str
    blocking: bool
    # Set by the loader. The writer picks the file name itself.
    body_file: str = ""


@dataclass(frozen=True, slots=True)
class Warning:
    version: int
    cause_id: str
    warned_at: datetime
    channel: str
    summary: str


@dataclass(frozen=True, slots=True)
class LoadedRecords:
    approvals: tuple[Approval, ...]
    reviews: tuple[Review, ...]
    warnings: tuple[Warning, ...]
    error: str | None


def state_dir(root: Path) -> Path:
    return root / "taskwatch" / "state"


class State:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.dir = state_dir(root)
        # flock on a second descriptor deadlocks this thread. Nested calls reuse
        # the owner only in this process; a forked child must take the file lock.
        self._held = threading.local()

    @property
    def approvals_path(self) -> Path:
        return self.dir / "approvals.jsonl"

    @property
    def reviews_path(self) -> Path:
        return self.dir / "reviews.jsonl"

    @property
    def bodies_dir(self) -> Path:
        return self.dir / "reviews" / "bodies"

    @property
    def warnings_path(self) -> Path:
        return self.dir / "warnings.jsonl"

    @contextmanager
    def lock(self) -> Iterator[None]:
        if getattr(self._held, "depth", 0) and getattr(self._held, "pid", None) == os.getpid():
            self._held.depth += 1
            try:
                yield
            finally:
                self._held.depth -= 1
            return
        _make_dirs(self.dir)
        handle = (self.dir / "lock").open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self._held.depth = 1
            self._held.pid = os.getpid()
            try:
                yield
            finally:
                self._held.depth = 0
                self._held.pid = None
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def append_start(self, approval_key: str, approved_at: datetime, approved: str) -> None:
        _check_id(approval_key, "approval_key")
        _check_text(approved, "approved")
        payload = {
            "version": RECORD_VERSION,
            "kind": "start",
            "approval_key": approval_key,
            "approved_at": _stamp(approved_at),
            "approved": approved,
        }
        with self.lock():
            records = _read_approvals(self.approvals_path)
            if any(item.kind == "start" and item.approval_key == approval_key for item in records):
                raise StateError("duplicate start")
            _append_line(self.approvals_path, payload, approved_at)

    def append_link(self, approval_key: str, card_id: str, now: datetime) -> None:
        _check_id(approval_key, "approval_key")
        _check_id(card_id, "card_id")
        with self.lock():
            _repair_torn(self.approvals_path, now)
            records = _read_approvals(self.approvals_path)
            if not any(
                item.kind == "start" and item.approval_key == approval_key for item in records
            ):
                raise StateError("link without start")
            linked = [
                item.card_id
                for item in records
                if item.kind == "link" and item.approval_key == approval_key
            ]
            if linked:
                if linked[0] != card_id or any(item != card_id for item in linked):
                    raise StateError("conflicting link")
                return
            _append_line(
                self.approvals_path,
                {
                    "version": RECORD_VERSION,
                    "kind": "link",
                    "approval_key": approval_key,
                    "card_id": card_id,
                },
                now,
            )

    def append_apply(
        self,
        approved: str,
        message_id: str,
        card_id: str,
        fingerprint: str,
        now: datetime,
    ) -> None:
        _check_text(approved, "approved")
        _check_id(message_id, "message_id")
        _check_id(card_id, "card_id")
        _check_text(fingerprint, "fingerprint")
        with self.lock():
            _append_line(
                self.approvals_path,
                {
                    "version": RECORD_VERSION,
                    "kind": "apply",
                    "approved": approved,
                    "message_id": message_id,
                    "card_id": card_id,
                    "fingerprint": fingerprint,
                },
                now,
            )

    def append_review(self, review: Review, body: str, now: datetime) -> Path:
        payload = _review_payload(review, body)
        with self.lock():
            if any(
                item.card == review.card and item.round == review.round
                for item in _read_reviews(self.reviews_path, self.bodies_dir)
            ):
                raise StateError("duplicate review")
            # The body is a new file that is never touched again. The line that
            # names it comes last, so a failure in between leaves only an unused file.
            name, digest = _write_body(self.bodies_dir, f"{review.card}-r{review.round}", body)
            payload |= {"body_file": name, "body_sha256": digest}
            _append_line(self.reviews_path, payload, now)
        return self.bodies_dir / name

    def append_warning(self, warning: Warning) -> None:
        if isinstance(warning.version, bool) or warning.version != RECORD_VERSION:
            raise StateError("unknown warning version")
        _check_id(warning.cause_id, "cause_id")
        if warning.channel not in {"internal", "user"}:
            raise StateError("unknown warning channel")
        _check_text(warning.summary, "summary")
        with self.lock():
            _append_line(
                self.warnings_path,
                {
                    "version": RECORD_VERSION,
                    "cause_id": warning.cause_id,
                    "warned_at": _stamp(warning.warned_at),
                    "channel": warning.channel,
                    "summary": warning.summary,
                },
                warning.warned_at,
            )

    def append_record(self, name: str, payload: Mapping[str, object], now: datetime) -> None:
        """One versioned JSON line in a state file that has no typed reader (runs, rejects)."""
        with self.lock():
            _append_line(self.dir / name, {"version": RECORD_VERSION, **payload}, now)

    def read_records(self, name: str) -> tuple[dict, ...]:
        """Committed lines of such a file. A bad line raises RecordError."""
        with self.lock():
            return _read_objects(self.dir / name)

    def load(self) -> LoadedRecords:
        try:
            with self.lock():
                return LoadedRecords(
                    _read_approvals(self.approvals_path),
                    _read_reviews(self.reviews_path, self.bodies_dir),
                    _read_warnings(self.warnings_path),
                    None,
                )
        except (RecordError, OSError) as exc:
            return LoadedRecords((), (), (), str(exc))


def _review_payload(review: Review, body: str) -> dict[str, object]:
    if isinstance(review.version, bool) or review.version != RECORD_VERSION:
        raise StateError("unknown review version")
    if isinstance(review.round, bool) or not isinstance(review.round, int) or review.round < 1:
        raise StateError("bad round")
    _check_id(review.card, "card")
    _check_text(review.fingerprint, "fingerprint")
    _check_text(review.basis, "basis")
    _check_text(review.reviewer, "reviewer")
    if review.verdict not in {"PASS", "FAIL"}:
        raise StateError("verdict must be PASS or FAIL")
    if not isinstance(review.blocking, bool):
        raise StateError("blocking must be a boolean")
    if not body.strip():
        raise StateError("review body is empty")
    return {
        "version": RECORD_VERSION,
        "round": review.round,
        "card": review.card,
        "fingerprint": review.fingerprint,
        "basis": review.basis,
        "reviewer": review.reviewer,
        "verdict": review.verdict,
        "blocking": review.blocking,
    }


def _append_line(path: Path, payload: Mapping[str, object], now: datetime) -> None:
    _repair_torn(path, now)
    _read_objects(path)
    line = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    _make_dirs(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        size = os.fstat(fd).st_size
        try:
            _write_all(fd, line)
            if size == 0:
                _publish(fd, path)
            else:
                os.fsync(fd)
        except BaseException:
            # A line that was not synced may still be visible. Cut back to the
            # size before this call and sync the cut, so the caller's failure and
            # the file agree after a restart. If the cut fails the line stays, with
            # a body that is already durable. If the sync of the cut fails, a
            # restart may bring the line back and a retry then sees a duplicate.
            with contextlib.suppress(OSError):
                os.ftruncate(fd, size)
                os.fsync(fd)
            raise
    finally:
        os.close(fd)


def _repair_torn(path: Path, now: datetime) -> Path | None:
    if not path.exists():
        return None
    data = path.read_bytes()
    if not data or data.endswith(b"\n"):
        return None
    newline = data.rfind(b"\n")
    fragment = data[newline + 1 :]
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dest = Path(f"{path}.torn-{stamp}")
    suffix = 1
    while dest.exists():
        dest = Path(f"{path}.torn-{stamp}-{suffix}")
        suffix += 1
    # Name the fragment durably before truncating. Rewriting the prefix would
    # empty the file first, so a crash there would drop confirmed lines.
    _create_new(dest, fragment, 0o644)
    keep = 0 if newline < 0 else newline + 1
    fd = os.open(path, os.O_RDWR)
    try:
        os.ftruncate(fd, keep)
        os.fsync(fd)
    finally:
        os.close(fd)
    return dest


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _make_dirs(path: Path) -> None:
    """Create missing directories; fsync the parent of every component, every call.

    A name is published only after its parent's fsync succeeded. A failed fsync
    leaves the directory in place, so a retry must sync the parent again instead
    of trusting that the directory exists.
    """
    for directory in reversed((path, *path.parents)):
        if directory.parent == directory:
            continue
        if not directory.exists():
            directory.mkdir(exist_ok=True)
        _fsync(directory.parent)


def _publish(fd: int, path: Path) -> None:
    """A new file is durable only when its bytes and the directory entry naming it are."""
    os.fsync(fd)
    _fsync(path.parent)


def _create_new(path: Path, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        _write_all(fd, data)
        _publish(fd, path)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _write_body(directory: Path, stem: str, body: str) -> tuple[str, str]:
    """Create one new body file, durable, and return its name and hash."""
    _make_dirs(directory)
    data = body.encode("utf-8")
    name = f"{stem}-{secrets.token_hex(8)}.md"
    _create_new(directory / name, data, 0o644)
    return name, hashlib.sha256(data).hexdigest()


def _committed(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RecordError(f"{path.name}: unreadable") from exc
    if not data:
        return ""
    if not data.endswith(b"\n"):
        newline = data.rfind(b"\n")
        data = b"" if newline < 0 else data[: newline + 1]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RecordError(f"{path.name}: not utf-8") from exc


def _read_objects(path: Path) -> tuple[dict, ...]:
    if not path.exists():
        return ()
    text = _committed(path)
    objects = []
    for lineno, line in enumerate(text.splitlines(), 1):
        where = f"{path.name}:{lineno}"
        if not line:
            raise RecordError(f"{where}: empty line")
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RecordError(f"{where}: not json") from exc
        if not isinstance(obj, dict):
            raise RecordError(f"{where}: not an object")
        version = obj.get("version")
        if isinstance(version, bool) or version != RECORD_VERSION:
            raise RecordError(f"{where}: unknown version")
        objects.append(obj)
    return tuple(objects)


def _read_named(path: Path, parse):
    indexed = enumerate(_read_objects(path), 1)
    return tuple(parse(obj, f"{path.name}:{index}") for index, obj in indexed)


def _read_approvals(path: Path) -> tuple[Approval, ...]:
    return _read_named(path, _approval)


def _read_reviews(path: Path, bodies: Path) -> tuple[Review, ...]:
    return _read_named(path, lambda obj, where: _review(obj, where, bodies))


def _read_warnings(path: Path) -> tuple[Warning, ...]:
    return _read_named(path, _warning)


def _approval(obj: dict, where: str) -> Approval:
    kind = obj.get("kind")
    if kind == "start":
        return _start(obj, where)
    if kind == "link":
        return _link(obj, where)
    if kind == "apply":
        return _apply(obj, where)
    raise RecordError(f"{where}: unknown kind")


def _start(obj: dict, where: str) -> Approval:
    _exact(obj, _START_KEYS, where)
    return Approval(
        version=RECORD_VERSION,
        kind="start",
        approval_key=_record_id(obj["approval_key"], where),
        approved_at=_parse_time(obj["approved_at"], where),
        approved=_text(obj["approved"], where),
    )


def _link(obj: dict, where: str) -> Approval:
    _exact(obj, _LINK_KEYS, where)
    return Approval(
        version=RECORD_VERSION,
        kind="link",
        approval_key=_record_id(obj["approval_key"], where),
        card_id=_record_id(obj["card_id"], where),
    )


def _apply(obj: dict, where: str) -> Approval:
    _exact(obj, _APPLY_KEYS, where)
    return Approval(
        version=RECORD_VERSION,
        kind="apply",
        approval_key=None,
        approved=_text(obj["approved"], where),
        message_id=_record_id(obj["message_id"], where),
        card_id=_record_id(obj["card_id"], where),
        fingerprint=_text(obj["fingerprint"], where),
    )


def _review(obj: dict, where: str, bodies: Path) -> Review:
    _exact(obj, _REVIEW_KEYS, where)
    verdict = obj["verdict"]
    if not isinstance(verdict, str) or verdict not in {"PASS", "FAIL"}:
        raise RecordError(f"{where}: unknown verdict")
    if not isinstance(obj["blocking"], bool):
        raise RecordError(f"{where}: blocking must be a boolean")
    body_file = _verified_body(bodies, obj["body_file"], obj["body_sha256"], where)
    return Review(
        version=RECORD_VERSION,
        round=_round(obj["round"], where),
        card=_record_id(obj["card"], where),
        fingerprint=_text(obj["fingerprint"], where),
        basis=_text(obj["basis"], where),
        reviewer=_text(obj["reviewer"], where),
        verdict=verdict,
        blocking=obj["blocking"],
        body_file=body_file,
    )


def _verified_body(bodies: Path, name: object, digest: object, where: str) -> str:
    # A line counts only with its body: a missing or changed body is not a review.
    if not isinstance(name, str) or _BODY.fullmatch(name) is None:
        raise RecordError(f"{where}: bad body name")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise RecordError(f"{where}: bad body hash")
    try:
        data = (bodies / name).read_bytes()
    except OSError as exc:
        raise RecordError(f"{where}: body unreadable") from exc
    if hashlib.sha256(data).hexdigest() != digest:
        raise RecordError(f"{where}: body does not match its hash")
    return name


def _warning(obj: dict, where: str) -> Warning:
    _exact(obj, _WARNING_KEYS, where)
    channel = obj["channel"]
    if not isinstance(channel, str) or channel not in {"internal", "user"}:
        raise RecordError(f"{where}: unknown channel")
    return Warning(
        version=RECORD_VERSION,
        cause_id=_record_id(obj["cause_id"], where),
        warned_at=_parse_time(obj["warned_at"], where),
        channel=channel,
        summary=_text(obj["summary"], where),
    )


def _exact(obj: dict, allowed: frozenset[str], where: str) -> None:
    missing = allowed - obj.keys()
    unknown = obj.keys() - allowed
    if missing or unknown:
        raise RecordError(f"{where}: unknown record")


def _check_id(value: object, where: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise StateError(f"{where}: bad id")
    return value


def _record_id(value: object, where: str) -> str:
    try:
        return _check_id(value, where)
    except StateError as exc:
        raise RecordError(str(exc)) from exc


def _check_text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\n" in value:
        raise StateError(f"{where}: bad text")
    return value


def _text(value: object, where: str) -> str:
    try:
        return _check_text(value, where)
    except StateError as exc:
        raise RecordError(str(exc)) from exc


def _round(value: object, where: str = "round") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RecordError(f"{where}: bad round")
    return value


def _stamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise StateError("time must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: object, where: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RecordError(f"{where}: bad time")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RecordError(f"{where}: bad time") from exc
