"""Block until a job needs attention. Read only: no lock, no write to the state folder."""

import json
import time
from collections.abc import Callable
from pathlib import Path

from .jobs import JobState, _digest, _validate_record
from .state import RECORD_VERSION, RecordError, state_dir

STOP_STAGES = frozenset({"approval_wait", "user_wait", "deliver_pending", "done", "cancelled"})
ATTENTION, TIMED_OUT, UNKNOWN = 0, 3, 2


class _Tail:
    """Follows jobs.jsonl by byte offset and keeps the newest snapshot of one job.

    Appends are whole lines and a torn tail has no newline, so a read without the writers'
    lock only ever sees committed lines. After the first pass a poll parses new lines only.
    """

    def __init__(self, path: Path, job_id: str) -> None:
        self.path, self.job_id = path, job_id
        self.offset = 0
        self.job: dict | None = None

    def poll(self) -> dict | None:
        try:
            with self.path.open("rb") as handle:
                size = handle.seek(0, 2)
                if size < self.offset:
                    self.offset, self.job = 0, None
                handle.seek(self.offset)
                data = handle.read(size - self.offset)
        except FileNotFoundError:
            return self.job
        except OSError as exc:
            raise RecordError(f"{self.path.name}: unreadable") from exc
        end = data.rfind(b"\n") + 1
        for line in data[:end].splitlines():
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise RecordError(f"{self.path.name}: not json") from exc
            if isinstance(row, dict) and row.get("version") != RECORD_VERSION:
                raise RecordError(f"{self.path.name}: unknown version")
            job = row.get("job") if isinstance(row, dict) else None
            if not isinstance(job, dict):
                raise RecordError(f"{self.path.name}: corrupt job snapshot")
            if job.get("id") != self.job_id:
                continue
            if row.get("sha256") != _digest(job):
                raise RecordError(f"{self.path.name}: corrupt job snapshot")
            _validate_record(job)
            self.job = job
        self.offset += end
        return self.job


def watch(
    root: Path,
    job_id: str,
    *,
    timeout: float,
    interval: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, str]:
    """Return (exit code, report). 0 = needs attention, 3 = time limit, 2 = unknown job."""
    tail = _Tail(state_dir(root.resolve()) / "jobs.jsonl", job_id)
    deadline = clock() + timeout
    while True:
        data = tail.poll()
        if data is None:
            return UNKNOWN, f"taskwatch: unknown job {job_id}"
        if data["stage"] in STOP_STAGES:
            return ATTENTION, _report(JobState(**data), root.resolve())
        remaining = deadline - clock()
        if remaining <= 0:
            return TIMED_OUT, (
                f"job {job_id}: still {data['stage']} after the watcher's time limit; "
                f"nothing to report. Start a new watcher if the work is still wanted."
            )
        sleep(min(interval, remaining))


def _report(job: JobState, root: Path) -> str:
    folder = state_dir(root) / "jobs" / job.id
    lines = [f"job {job.id}: {job.stage}"]
    if job.wait_reason:
        lines.append(f"reason: {_line(job.wait_reason)}")
    if job.error:
        lines.append("error: recorded; it may hold command output, so read it with show")
    review = job.reviews[-1] if job.reviews else None
    if review and review.get("verdict"):
        lines.append(f"verdict: {_line(review['verdict'])}")
    if job.outputs:
        base = Path(job.workspace) if job.workspace else None
        for output in job.outputs[:5]:
            lines.append(f"output: {_line(base / output if base else output)}")
    lines.append(f"details: {_line(folder)}")
    lines.append(f"next: taskwatch --root {_line(root)} show {job.id}")
    if job.stage == "approval_wait":
        lines.append(f"then: taskwatch --root {_line(root)} approve {job.id} --reason <source>")
    elif job.stage == "user_wait":
        lines.append(f"then: taskwatch --root {_line(root)} resume {job.id} --reason <source>")
    lines.append("This report is a hint; re-read the state before acting.")
    return "\n".join(lines[:20])


def _line(value: object) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= 300 else text[:297] + "..."
