"""Timer scan for the independent job board: admit, launch, recover, stop.

One scan holds the State lock end to end, so a runner service cannot change a job while the
scan decides on its snapshot. A step is one systemd service. The runner marks a finished step
by setting ``ended_at`` together with the next stage; the scan then launches that next step
under a new attempt while the job keeps its slot and target.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from .jobs import progress_fingerprint

SLOT_STAGES = frozenset({"work", "check", "review", "fix"})
DONE_STAGES = frozenset({"deliver_pending", "done"})
MAX_RECOVERIES = 3
MAX_FIX_ROUNDS = 3
DEAD = frozenset({"inactive", "missing"})
LIVE_STATES = frozenset({"active", "activating", "reloading", "deactivating"})
_SPAWN_ERRORS = (OSError, RuntimeError, subprocess.SubprocessError)
_INPUT_ERRORS = (ValueError, OSError, KeyError)
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
CGROUP_ROOT = Path("/sys/fs/cgroup")


class Systemd:
    """User systemd. Anything not proven dead or alive is reported as unknown."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def status(self, unit: str) -> str:
        info = self._show(unit)
        if info is None:
            return "unknown"
        if info.get("LoadState") == "not-found":
            return "missing"
        state = info.get("ActiveState", "")
        # A stray process left in the cgroup means the step is not dead.
        if state in LIVE_STATES or not _cgroup_empty(info.get("ControlGroup", "")):
            return "active"
        return "inactive" if state in ("inactive", "failed") else "unknown"

    def start(self, job: dict) -> None:
        argv = [
            "systemd-run",
            "--user",
            "--quiet",
            f"--unit={job['unit']}",
            "--property=KillMode=control-group",
            sys.executable,
            "-m",
            "taskwatch.job_cli",
            "--root",
            str(self.root),
            "run",
            job["id"],
            "--attempt",
            str(job["attempt"]),
        ]
        subprocess.run(argv, check=True, capture_output=True, timeout=30)

    def stop(self, unit: str) -> bool:
        try:
            subprocess.run(
                ["systemctl", "--user", "stop", unit],
                check=False,
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return self.status(unit) in DEAD

    def _show(self, unit: str) -> dict[str, str] | None:
        argv = ["systemctl", "--user", "show", unit]
        argv += ["-p", "LoadState", "-p", "ActiveState", "-p", "ControlGroup"]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        info = dict(line.partition("=")[::2] for line in proc.stdout.splitlines())
        # systemctl may exit nonzero for a unit it reports as not-found; that is a clear answer.
        if proc.returncode != 0 and info.get("LoadState") != "not-found":
            return None
        return info


def _cgroup_empty(cgroup: str) -> bool:
    """True only when neither the group nor any descendant group holds a process."""
    if not cgroup:
        return True
    base = CGROUP_ROOT / cgroup.lstrip("/")
    try:
        if "populated 1" in (base / "cgroup.events").read_text(encoding="utf-8").splitlines():
            return False
    except FileNotFoundError:
        pass
    except OSError:
        return False
    try:
        return not any(p.read_text(encoding="utf-8").strip() for p in base.rglob("cgroup.procs"))
    except OSError:
        return False


def scan_jobs(
    jobs,
    config,
    *,
    now: float | None = None,
    boot_id: str | None = None,
    service=None,
) -> list[str]:
    """Run one scan and return the IDs of jobs whose step was launched.

    A damaged journal raises before any job is touched, so nothing starts on bad data.
    """
    scan = _Scan(
        jobs,
        config,
        time.time() if now is None else now,
        boot_id if boot_id is not None else BOOT_ID_PATH.read_text(encoding="utf-8").strip(),
        service if service is not None else Systemd(jobs.state.root),
    )
    with jobs.state.lock():
        return scan.run()


class _Scan:
    def __init__(self, jobs, config, now: float, boot: str, service) -> None:
        self.jobs = jobs
        self.config = config
        self.now = now
        self.boot = boot
        self.service = service
        self.launched: list[str] = []
        self.all: dict[str, dict] = {}
        self.order: list[dict] = []

    def run(self) -> list[str]:
        self.all = self.jobs.load()
        self.order = sorted(self.all.values(), key=_rank)
        for job in self.order:
            self._tend(job)
        for job in self.order:
            if _is_gap(job) and self._confirm_dead(job):
                self._launch(job, job["stage"])
        for job in self.order:
            if _is_pending(job):
                self._admit(job)
        return self.launched

    # -- existing jobs ----------------------------------------------------------------------

    def _tend(self, job: dict) -> None:
        if job["stage"] in SLOT_STAGES:
            if job["unit"] and job["ended_at"] <= 0:
                self._watch(job)
        elif job["unit"]:
            self._release(job)
        if job["stage"] == "queued" and job.get("approval_required"):
            job.update(stage="approval_wait", resume_stage="work", wait_reason="approval_required")
            self._event(job, "approval_wait")
            self.jobs.save(job, self.now)

    def _confirm_dead(self, job: dict) -> bool:
        """True when the job's whole old service is dead; never assumed, even after a reboot."""
        if self.service.status(job["unit"]) in DEAD or self.service.stop(job["unit"]):
            return True
        if job["error"] != "stop_unconfirmed":
            job["error"] = "stop_unconfirmed"
            self.jobs.save(job, self.now)
        return False

    def _release(self, job: dict) -> None:
        """Free a unit that no stage owns (cancel, wait, done, re-queue) once it is dead.

        The slot and target stay held until the whole service is confirmed stopped.
        """
        if self._confirm_dead(job):
            job["unit"] = ""
            self._event(job, "unit_released")
            self.jobs.save(job, self.now)

    def _watch(self, job: dict) -> None:
        if job["boot_id"] and job["boot_id"] != self.boot:
            self._event(job, "reboot_interrupted")
            self._recover(job)
            return
        status = self.service.status(job["unit"])
        if status in DEAD:
            self._event(job, "service_dead")
            self._recover(job)
        elif status == "active":
            self._check_stall(job)
        else:
            self._park(job, "service_unknown")

    def _check_stall(self, job: dict) -> None:
        self._refresh_progress(job)
        seen = max(job["last_progress_at"], job["evidence_at"])
        stalled = self.now - seen >= self.config.stall_seconds + job.get("expected_wait_seconds", 0)
        if not stalled:
            if job["stall_suspected"]:
                job["stall_suspected"] = False
                self.jobs.save(job, self.now)
        elif not job["stall_suspected"]:
            job["stall_suspected"] = True
            self._event(job, "stall_suspected")
            self.jobs.save(job, self.now)
        elif self.service.stop(job["unit"]):
            self._event(job, "stall_stopped")
            self._recover(job)
        else:
            self._park(job, "stall_stop_unconfirmed")

    def _refresh_progress(self, job: dict) -> None:
        if not job["workspace"]:
            return  # the runner has not prepared it yet; nothing to compare
        digest = progress_fingerprint(job, Path(job["workspace"]))
        if digest == job["progress_hash"]:
            return
        # The first hash is only a baseline: absent files are not progress.
        if job["progress_hash"]:
            job["last_progress_at"] = self.now
        job["progress_hash"] = digest
        self.jobs.save(job, self.now)

    def _recover(self, job: dict) -> None:
        if job["external_pending"]:
            self._park(job, "external_pending")
        elif job["stage"] == "fix" and job["fix_rounds"] > 0:
            self._retry_review_fix(job)
        elif job["recoveries"] >= MAX_RECOVERIES:
            self._park(job, "recoveries_exhausted")
        elif self._confirm_dead(job):
            job["recoveries"] += 1
            self._launch(job, job["stage"])

    def _retry_review_fix(self, job: dict) -> None:
        """A failed fix after a review counts as a round: the cap bounds model invocations."""
        if job["fix_rounds"] >= MAX_FIX_ROUNDS:
            self._park(job, "review_exhausted")
        elif self._confirm_dead(job):
            job["fix_rounds"] += 1
            self._launch(job, "fix")

    # -- launching --------------------------------------------------------------------------

    def _admit(self, job: dict) -> None:
        reason = self._blocker(job)
        if reason is None:
            self._launch(job, _launch_stage(job))
        elif job["stage"] == "queued" and job["wait_reason"] != reason:
            job["wait_reason"] = reason
            self.jobs.save(job, self.now)

    def _blocker(self, job: dict) -> str | None:
        if job["unit"]:
            return "unit"
        for pred in job["predecessors"]:
            if self.all.get(pred, {}).get("stage") not in DONE_STAGES:
                return f"predecessor:{pred}"
        slots, targets = self._busy()
        if job.get("target") and job["target"] in targets:
            return "target"
        return "slot" if slots >= self.config.slots else None

    def _busy(self) -> tuple[int, set[str]]:
        held = [job for job in self.all.values() if _holds(job)]
        return len(held), {job["target"] for job in held if job.get("target")}

    def _launch(self, job: dict, stage: str) -> None:
        try:
            self.jobs.verify_inputs(job)
        except _INPUT_ERRORS as exc:
            self._park(job, "inputs_invalid", str(exc))
            return
        job["attempt"] += 1
        job.update(
            stage=stage,
            unit=f"taskwatch-{job['id']}-a{job['attempt']}.service",
            boot_id=self.boot,
            started_at=self.now,
            ended_at=0.0,
            last_progress_at=self.now,
            stall_suspected=False,
            wait_reason="",
            error="",
        )
        # Reserve before the service call: a lost ack is found later by unit name.
        self.jobs.save(job, self.now)
        self.launched.append(job["id"])
        try:
            self.service.start(job)
        except _SPAWN_ERRORS as exc:
            job["error"] = f"start_failed: {exc}"
            self._event(job, "start_failed")
            self.jobs.save(job, self.now)

    def _park(self, job: dict, reason: str, error: str = "") -> None:
        """Wait for the user. The unit is kept until it is confirmed dead, so slot and target
        are never freed while a process may still run."""
        if job["stage"] in SLOT_STAGES:
            job["resume_stage"] = job["stage"]
        job.update(stage="user_wait", wait_reason=reason, error=error or reason, ended_at=self.now)
        self._event(job, "parked", reason=reason)
        self.jobs.save(job, self.now)
        if job["unit"]:
            self._release(job)

    def _event(self, job: dict, kind: str, **extra: object) -> None:
        event = {"source": "scan", "kind": kind, "at": self.now, "attempt": job["attempt"]}
        job["events"].append({**event, **extra})


def _rank(job: dict) -> tuple[float, str]:
    return job["last_progress_at"], job["id"]


def _holds(job: dict) -> bool:
    # Any recorded unit may still be running, whatever the stage says, so it holds.
    return bool(job["unit"])


def _launch_stage(job: dict) -> str:
    if job["stage"] in SLOT_STAGES:
        return job["stage"]
    resume = job["resume_stage"]
    return resume if resume in SLOT_STAGES else "work"


def _is_gap(job: dict) -> bool:
    return job["stage"] in SLOT_STAGES and bool(job["unit"]) and job["ended_at"] > 0


def _is_pending(job: dict) -> bool:
    return job["stage"] == "queued" or (job["stage"] in SLOT_STAGES and not job["unit"])
