"""Send a checked result once; uncertain external outcomes need a user decision."""

import fcntl
import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path

from .job_runner import _kill_group, _write_durable
from .jobs import JobConfig, Jobs, JobState

DELIVERY_TIMEOUT = 60


def deliver_job(jobs: Jobs, config: JobConfig, job: JobState, now: float) -> JobState:
    """Called under the state lock, after the old runner service has stopped."""
    if job.deliveries and job.deliveries[-1]["status"] == "started":
        record = {**job.deliveries[-1], "status": "unknown", "error": "delivery interrupted"}
        return _finish(jobs, job, record, now)
    # Inherit this lock into the sender so even a killed scanner cannot overlap an orphan send.
    with (jobs.folder(job) / "delivery.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return job
        try:
            message = _message(jobs, job)
        except OSError as exc:
            stopped = replace(
                job,
                stage="user_wait",
                resume_stage="deliver_pending",
                wait_reason="delivery_unreadable",
                error=str(exc),
            )
            jobs.save(stopped, now)
            return stopped
        command = [str(message) if part == "{message}" else part for part in config.deliver_command]
        record = {
            "at": now,
            "command": command,
            "message_file": str(message),
            "message_sha256": hashlib.sha256(message.read_bytes()).hexdigest(),
            "status": "started",
            "exit_code": None,
            "message_id": None,
            "stdout": "",
            "stderr": "",
            "error": "",
        }
        job = replace(job, deliveries=(*job.deliveries, record))
        jobs.save(job, now)
        return _finish(jobs, job, {**record, **_send(command, lock.fileno())}, now)


def _message(jobs: Jobs, job: JobState) -> Path:
    folder = jobs.folder(job)
    review = job.reviews[-1] if job.reviews else {}
    review_file = folder / "delivery-review.txt"
    _write_durable(review_file, review.get("body", ""))
    lines = [
        f"Job {job.id}: {job.prompt.strip().splitlines()[0]}",
        f"Verdict: {review.get('verdict', 'unavailable')}",
        "Outputs:",
        *(f"- {Path(job.workspace) / name}" for name in job.outputs),
        f"Result: {job.workspace}",
        f"Review: {review_file}",
    ]
    if job.checks:
        lines.append(f"Check: {job.checks[-1]['log']}")
    message = folder / "delivery.txt"
    _write_durable(message, "\n".join(lines) + "\n")
    return message


def _send(command: list[str], lock_fd: int) -> dict:
    result = {
        "status": "unknown",
        "exit_code": None,
        "message_id": None,
        "stdout": "",
        "stderr": "",
        "error": "",
    }
    try:
        with subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            pass_fds=(lock_fd,),
        ) as proc:
            try:
                out, err = proc.communicate(timeout=DELIVERY_TIMEOUT)
            except subprocess.TimeoutExpired:
                _kill_group(proc.pid)
                out, err = proc.communicate()
                result["error"] = "delivery timed out"
            finally:
                _kill_group(proc.pid)
            result.update(
                exit_code=proc.returncode,
                stdout=out.decode("utf-8", errors="backslashreplace"),
                stderr=err.decode("utf-8", errors="backslashreplace"),
            )
        if result["error"]:
            return result
        payload = json.loads(out.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("delivery response is not an object")
        result["message_id"] = payload.get("message_id")
        if (
            proc.returncode == 0
            and payload.get("success")
            and not payload.get("error")
            and (payload.get("message_id") is not None)
        ):
            result["status"] = "success"
        elif proc.returncode > 0 and payload.get("error") and not payload.get("success"):
            result.update(status="failure", error=str(payload["error"]))
        else:
            result["error"] = "delivery response does not confirm sending or definite failure"
    except (OSError, ValueError, UnicodeError) as exc:
        result["error"] = str(exc)
    return result


def _finish(jobs: Jobs, job: JobState, record: dict, now: float) -> JobState:
    status = record["status"]
    failures = job.delivery_failures + 1 if status == "failure" else 0
    reason = "delivery_unknown" if status == "unknown" else "delivery_failed"
    stopped = status == "unknown" or failures >= 3
    job = replace(
        job,
        deliveries=(*job.deliveries[:-1], record),
        delivery_failures=failures,
        stage="done" if status == "success" else "user_wait" if stopped else "deliver_pending",
        resume_stage="deliver_pending",
        wait_reason=reason if stopped else "",
        error=record["error"],
        ended_at=now,
    )
    jobs.save(job, now)
    return job
