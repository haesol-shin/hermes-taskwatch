"""Durable job snapshots sharing the existing state lock and JSONL publication."""

import hashlib
import json
import math
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

from .state import RecordError, State, _create_new, _make_dirs

STAGES = frozenset(
    {
        "queued",
        "work",
        "check",
        "review",
        "fix",
        "approval_wait",
        "deliver_pending",
        "done",
        "user_wait",
        "cancelled",
    }
)
KINDS = frozenset({"code", "design", "document", "research"})
MAX_FIX_ROUNDS = 3
MAX_RECOVERIES = 3


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _json_value(value):
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class JobState:
    id: str
    receipt_key: str
    approval_source: str
    approval_scope: str
    prompt: str
    kind: str
    target: str
    intent: str
    spec: str
    outputs: tuple[str, ...]
    check_command: tuple[str, ...]
    checklist: str
    progress: str
    inputs: Mapping[str, tuple[str, ...]]
    input_hashes: Mapping[str, str]
    predecessors: tuple[str, ...]
    expected_wait_seconds: int
    approval_required: bool
    stage: str = "queued"
    wait_reason: str = ""
    attempt: int = 0
    unit: str = ""
    boot_id: str = ""
    session_path: str = ""
    session_dir: str = ""
    workspace: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    last_progress_at: float = 0.0
    progress_hash: str = ""
    evidence_at: float = 0.0
    stall_suspected: bool = False
    recoveries: int = 0
    fix_rounds: int = 0
    resume_stage: str = "work"
    events: tuple[Mapping, ...] = ()
    checks: tuple[Mapping, ...] = ()
    reviews: tuple[Mapping, ...] = ()
    fingerprint: str = ""
    basis: str = ""
    error: str = ""
    guideline_hashes: Mapping[str, str] | None = None
    deliveries: tuple[Mapping, ...] = ()
    delivery_failures: int = 0

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, (Mapping, list, tuple)):
                object.__setattr__(self, field.name, _freeze(value))

    def to_json(self) -> dict:
        return {field.name: _json_value(getattr(self, field.name)) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class JobConfig:
    slots: int = 2
    stall_seconds: int = 1800
    omp_command: tuple[str, ...] = ("omp",)
    rpc_timeout: int = 3600
    deliver_command: tuple[str, ...] = ("false", "{message}")


def load_job_config(root: Path) -> JobConfig:
    path = root / "taskwatch" / "jobs-config.json"
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict) or set(obj) != {
        "version",
        "slots",
        "stall_seconds",
        "omp_command",
        "rpc_timeout",
        "deliver_command",
    }:
        raise ValueError(
            "jobs config needs version, slots, stall_seconds, omp_command, rpc_timeout, "
            "deliver_command"
        )
    if type(obj["version"]) is not int or obj["version"] != 1:
        raise ValueError("unknown jobs config version")
    for key in ("slots", "stall_seconds", "rpc_timeout"):
        if type(obj[key]) is not int or obj[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    _strings(obj["omp_command"], "omp_command", nonempty=True)
    _strings(obj["deliver_command"], "deliver_command", nonempty=True)
    command = obj["deliver_command"]
    if command.count("{message}") != 1 or any(
        "{message}" in part and part != "{message}" for part in command
    ):
        raise ValueError("deliver_command needs exactly one whole-argument {message}")
    return JobConfig(
        obj["slots"],
        obj["stall_seconds"],
        tuple(obj["omp_command"]),
        obj["rpc_timeout"],
        tuple(command),
    )


def _digest(obj: dict) -> str:
    data = json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(data).hexdigest()


def _stamp(now: float) -> datetime:
    return datetime.fromtimestamp(now, timezone.utc)


class Jobs:
    def __init__(self, root: Path) -> None:
        self.state = State(root.resolve())

    def folder(self, job: JobState) -> Path:
        return self.state.dir / "jobs" / job.id

    def load(self) -> dict[str, JobState]:
        with self.state.lock():
            latest = {}
            receipts = {}
            for row in self.state.read_records("jobs.jsonl"):
                job = row.get("job")
                if not isinstance(job, dict) or row.get("sha256") != _digest(job):
                    raise RecordError("jobs.jsonl: corrupt job snapshot")
                _validate_record(job)
                key, identifier = job["receipt_key"], job["id"]
                if key in receipts and receipts[key] != identifier:
                    raise RecordError("jobs.jsonl: conflicting receipt key")
                receipts[key] = identifier
                try:
                    latest[identifier] = JobState(**job)
                except TypeError as exc:
                    raise RecordError(f"jobs.jsonl: invalid job fields: {exc}") from exc
            return latest

    def save(self, job: JobState, now: float) -> None:
        data = job.to_json()
        _validate_record(data)
        with self.state.lock():
            self.load()
            self.state.append_record(
                "jobs.jsonl", {"job": data, "sha256": _digest(data)}, _stamp(now)
            )

    def submit(self, payload: dict, now: float | None = None) -> JobState:
        now = time.time() if now is None else now
        if not isinstance(payload, dict) or not isinstance(payload.get("receipt_key"), str):
            raise ValueError("submit needs a receipt_key")
        with self.state.lock():
            existing = self.load()
            for job in existing.values():
                if job.receipt_key == payload["receipt_key"]:
                    return job
            data = _submission(payload)
            missing = set(data["predecessors"]) - existing.keys()
            if missing:
                raise ValueError(f"unknown predecessors: {', '.join(sorted(missing))}")
            data.update(id=uuid.uuid4().hex, last_progress_at=now)
            self._copy_inputs(data)
            job = JobState(**data)
            self.save(job, now)
            return job

    def _copy_inputs(self, job: dict) -> None:
        directory = self.state.dir / "jobs" / job["id"] / "inputs"
        _make_dirs(directory)
        hashes = {}
        for role, sources in {
            "intent": [job["intent"]],
            "spec": [job["spec"]],
            **job["inputs"],
        }.items():
            copied = []
            for index, source in enumerate(sources):
                data = Path(source).read_bytes()
                dest = directory / f"{role}-{index}.txt"
                _create_new(dest, data, 0o600)
                hashes[str(dest)] = hashlib.sha256(data).hexdigest()
                copied.append(str(dest))
            if role in {"intent", "spec"}:
                job[role] = copied[0]
            else:
                job["inputs"][role] = copied
        job["input_hashes"] = hashes

    def verify_inputs(self, job: JobState) -> None:
        for name, digest in job.input_hashes.items():
            if hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest:
                raise ValueError("copied input does not match its hash")


def _strings(value: object, label: str, *, nonempty: bool = False) -> None:
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{label} must be {'a nonempty' if nonempty else 'a'} list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{label} needs nonempty strings")


def _relative(value: object, label: str) -> None:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ValueError(f"{label} must be a relative path")
    if ".." in Path(value).parts or value == ".":
        raise ValueError(f"{label} must stay in the workspace")


def _submission(payload: dict) -> dict:
    required = {
        "receipt_key",
        "approval_source",
        "approval_scope",
        "prompt",
        "kind",
        "target",
        "intent",
        "spec",
        "outputs",
        "check_command",
        "checklist",
        "progress",
    }
    optional = {"inputs", "predecessors", "expected_wait_seconds", "approval_required"}
    if required - payload.keys() or payload.keys() - required - optional:
        raise ValueError("submit has missing or unknown fields")
    data = {**payload}
    for key in required - {"outputs", "check_command"}:
        if not isinstance(data[key], str) or not data[key].strip():
            raise ValueError(f"{key} must be nonempty text")
    if data["kind"] not in KINDS:
        raise ValueError("unknown job kind")
    for key in ("outputs", "check_command"):
        _strings(data[key], key, nonempty=True)
    for item in data["outputs"]:
        _relative(item, "output")
    for key in ("checklist", "progress"):
        _relative(data[key], key)
    data["target"] = str(Path(data["target"]).resolve(strict=True))
    if not Path(data["target"]).is_dir():
        raise ValueError("target must be a directory")
    data.setdefault("predecessors", [])
    _strings(data["predecessors"], "predecessors")
    data.setdefault("expected_wait_seconds", 0)
    data.setdefault("approval_required", False)
    if type(data["expected_wait_seconds"]) is not int or data["expected_wait_seconds"] < 0:
        raise ValueError("expected_wait_seconds must be a nonnegative integer")
    if type(data["approval_required"]) is not bool:
        raise ValueError("approval_required must be boolean")
    inputs = data.get("inputs", {})
    if not isinstance(inputs, dict) or inputs.keys() - {"work", "review"}:
        raise ValueError("inputs must map work/review to file lists")
    for role, paths in inputs.items():
        _strings(paths, role)
    data["inputs"] = {role: list(paths) for role, paths in inputs.items()}
    return data


def _validate_record(job: dict) -> None:
    if not isinstance(job.get("id"), str) or not re.fullmatch(r"[0-9a-f]{32}", job["id"]):
        raise RecordError("jobs.jsonl: invalid job id")
    if job.get("stage") not in STAGES or job.get("kind") not in KINDS:
        raise RecordError("jobs.jsonl: unknown stage or kind")
    for key in ("attempt", "recoveries", "fix_rounds"):
        if type(job.get(key)) is not int or job[key] < 0:
            raise RecordError(f"jobs.jsonl: invalid {key}")
    for key in (
        "receipt_key",
        "approval_source",
        "approval_scope",
        "prompt",
        "target",
        "intent",
        "spec",
        "checklist",
        "progress",
    ):
        if not isinstance(job.get(key), str) or not job[key]:
            raise RecordError(f"jobs.jsonl: missing {key}")
    for key in ("outputs", "check_command", "predecessors", "events", "checks", "reviews"):
        if not isinstance(job.get(key), list):
            raise RecordError(f"jobs.jsonl: invalid {key}")
    if not isinstance(job.get("input_hashes"), dict):
        raise RecordError("jobs.jsonl: missing input hashes")
    for key in ("started_at", "ended_at", "last_progress_at", "evidence_at"):
        if type(job.get(key)) not in {int, float} or not math.isfinite(job[key]):
            raise RecordError(f"jobs.jsonl: invalid {key}")
    _validate_controls(job)
    _validate_manifest(job)
    if type(job.get("delivery_failures", 0)) is not int or job.get("delivery_failures", 0) < 0:
        raise RecordError("jobs.jsonl: invalid delivery failures")
    deliveries = job.get("deliveries", [])
    if not isinstance(deliveries, list) or any(
        not isinstance(row, dict)
        or row.get("status") not in {"started", "success", "failure", "unknown"}
        for row in deliveries
    ):
        raise RecordError("jobs.jsonl: invalid delivery attempts")


def _validate_controls(job: dict) -> None:
    for key in ("approval_required", "stall_suspected"):
        if type(job.get(key)) is not bool:
            raise RecordError(f"jobs.jsonl: invalid {key}")
    for key in (
        "wait_reason",
        "unit",
        "boot_id",
        "session_path",
        "session_dir",
        "workspace",
        "progress_hash",
        "fingerprint",
        "basis",
        "error",
        "resume_stage",
    ):
        if not isinstance(job.get(key), str):
            raise RecordError(f"jobs.jsonl: invalid {key}")
    if job["resume_stage"] not in STAGES:
        raise RecordError("jobs.jsonl: invalid resume stage")
    if type(job.get("expected_wait_seconds")) is not int or job["expected_wait_seconds"] < 0:
        raise RecordError("jobs.jsonl: invalid expected wait")
    try:
        for key in ("outputs", "check_command", "predecessors"):
            _strings(job[key], key, nonempty=key != "predecessors")
        for relative in [*job["outputs"], job["checklist"], job["progress"]]:
            _relative(relative, "job file")
    except ValueError as exc:
        raise RecordError(f"jobs.jsonl: {exc}") from exc


def _validate_manifest(job: dict) -> None:
    inputs = job.get("inputs")
    if not isinstance(inputs, dict) or inputs.keys() - {"work", "review"}:
        raise RecordError("jobs.jsonl: invalid role inputs")
    expected = {job["intent"], job["spec"]}
    for role, paths in inputs.items():
        try:
            _strings(paths, role)
        except ValueError as exc:
            raise RecordError(f"jobs.jsonl: {exc}") from exc
        expected.update(paths)
    hashes = job["input_hashes"]
    if hashes.keys() != expected or any(
        not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
        for value in hashes.values()
    ):
        raise RecordError("jobs.jsonl: incomplete input hash manifest")


def progress_fingerprint(job: JobState, workspace: Path) -> str:
    """Hash progress and outputs, never growing session or command logs."""
    workspace = workspace.resolve()
    digest = hashlib.sha256()
    for relative in [job.progress, *job.outputs]:
        path = (workspace / relative).resolve()
        inside = not Path(relative).is_absolute() and path.is_relative_to(workspace)
        data = path.read_bytes() if inside and path.is_file() else b""
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()
