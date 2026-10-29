"""Send a checked result once; uncertain external outcomes need a user decision."""

import fcntl
import hashlib
import json
import shlex
import subprocess
from dataclasses import replace
from pathlib import Path

from .evidence import basis_of
from .job_runner import _fingerprint, _kill_group, _requeue_check, _write_durable
from .jobs import JobConfig, Jobs, JobState

DELIVERY_TIMEOUT = 60


def deliver_job(jobs: Jobs, config: JobConfig, job: JobState, now: float) -> JobState:
    """Called under the state lock, after the old runner service has stopped."""
    if job.deliveries and job.deliveries[-1]["status"] == "started":
        record = {**job.deliveries[-1], "status": "unknown", "error": "delivery interrupted"}
        return _finish(jobs, job, record, now)
    review = job.reviews[-1] if job.reviews else {}
    try:
        current = (
            review.get("ok")
            and review.get("verdict") == "PASS"
            and _fingerprint(job, Path(job.workspace)) == review.get("fingerprint")
            and bool(job.guideline_hashes)
            and basis_of(
                Path(job.intent), Path(job.spec), tuple(Path(p) for p in job.guideline_hashes)
            )
            == review.get("basis")
        )
    except (ValueError, OSError):
        current = False
    if not current:
        job = replace(job, **_requeue_check(job))
        jobs.save(job, now)
        return job
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
        else:
            result["error"] = str(payload.get("error") or "delivery response does not confirm send")
    except (OSError, ValueError, UnicodeError) as exc:
        result["error"] = str(exc)
    return result


def _finish(jobs: Jobs, job: JobState, record: dict, now: float) -> JobState:
    success = record["status"] == "success"
    job = replace(
        job,
        deliveries=(*job.deliveries[:-1], record),
        stage="done" if success else "user_wait",
        resume_stage="deliver_pending",
        wait_reason="" if success else "delivery_unknown",
        error=record["error"],
        ended_at=now,
    )
    jobs.save(job, now)
    return job


def notify_wait(jobs: Jobs, config: JobConfig, job: JobState, now: float) -> None:
    """Attempt once per wait entry, independently of result delivery."""
    # Only resume/approve can leave a wait for another runnable cycle.
    entry = sum(event.get("type") in {"resume", "approve"} for event in job.events)
    if job.notices and job.notices[-1]["entry"] == entry:
        if job.notices[-1]["status"] == "started":
            record = {**job.notices[-1], "status": "unknown", "error": "notice interrupted"}
            jobs.save(replace(job, notices=(*job.notices[:-1], record)), now)
        return
    message = jobs.folder(job) / f"notice-{len(job.notices)}.txt"
    command = [str(message) if part == "{message}" else part for part in config.deliver_command]
    record = {
        "at": now,
        "entry": entry,
        "stage": job.stage,
        "wait_reason": job.wait_reason,
        "command": command,
        "message_file": str(message),
        "status": "started",
    }
    job = replace(job, notices=(*job.notices, record))
    jobs.save(job, now)
    try:
        _write_durable(
            message,
            "\n".join(
                [
                    f"Job {job.id}: {job.prompt.strip().splitlines()[0]}",
                    f"Stage: {job.stage}; wait_reason: {job.wait_reason}",
                    f"Error: {job.error}",
                    *(
                        shlex.join(
                            [
                                "taskwatch",
                                "--root",
                                str(jobs.state.root),
                                command,
                                job.id,
                                "--reason",
                                "<reason>",
                            ]
                        )
                        for command in ("resume", "approve", "cancel")
                    ),
                ]
            )
            + "\n",
        )
        record["message_sha256"] = hashlib.sha256(message.read_bytes()).hexdigest()
        jobs.save(replace(job, notices=(*job.notices[:-1], record)), now)
        with message.open("rb") as lock:
            record.update(_send(command, lock.fileno()))
    except OSError as exc:
        record.update(status="unknown", error=str(exc))
    jobs.save(replace(job, notices=(*job.notices[:-1], record)), now)
