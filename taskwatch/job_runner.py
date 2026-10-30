"""Run one step of a job: a work or fix session, the deterministic check, or a fresh review.

One service runs one step and exits. Stage changes are compare-and-set on the attempt and
the step's stage under the state lock, so output of an old attempt cannot change a job.
A step that fails without a routed outcome records the error and leaves the stage, so the
scan resumes it. Nothing here calls a model except the omp RPC sessions of work, fix and review.
"""

import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from .checks import checklist_problems
from .evidence import basis_of
from .executor import _verdict_line
from .fingerprint import (
    FingerprintError,
    _git_ok,
    files_fingerprint,
    head_sha,
    uncommitted_changes,
)
from .job_env import job_environment
from .job_rpc import RpcAborted, RpcClient, RpcError
from .jobs import MAX_FIX_ROUNDS, MAX_RECOVERIES, JobConfig, Jobs, JobState, progress_fingerprint
from .review_docs import docs_dir, profile_names

STEPS = frozenset({"work", "fix", "check", "review"})
EVIDENCE_EVERY = 15.0
SETTLE_POLL = 2.0
WATCH_EVERY = 2.0
_EVENTS = frozenset(
    {"agent_start", "agent_end", "prompt_result", "session_settled", "subagent_lifecycle"}
)
_PROGRESS_FRAMES = frozenset({"message_end", "tool_execution_end", "turn_end", "subagent_progress"})
_EVENT_FIELDS = (
    "id",
    "sessionFile",
    "parentToolCallId",
    "subagentId",
    "status",
    "agentInvoked",
    "sessionSettled",
    "isTerminal",
    "yielded",
)
# The one verdict problem a review-only job accepts: a FAIL is its result, not a retry.
_FAIL_PROBLEM = "the reviewer did not pass the result"


def _now() -> float:
    return time.time()


class StepError(Exception):
    """The step failed in a way the scan can resume. The message is recorded."""


class InputsChanged(StepError):
    """A copied input or review profile no longer matches its recorded hash."""


class StaleAttempt(Exception):
    """The job moved on. This process must stop without writing."""


@dataclass(frozen=True, slots=True)
class Ctx:
    jobs: Jobs
    config: JobConfig
    job: JobState
    folder: Path
    workspace: Path

    @property
    def stage(self) -> str:
        return self.job.stage

    @property
    def env(self) -> dict[str, str]:
        return job_environment(self.config.env)


def run_job(jobs: Jobs, config: JobConfig, job_id: str, attempt: int) -> int:
    """0: step done and outcome recorded. 1: failure recorded. 2: stale attempt, nothing written."""
    job = jobs.load().get(job_id)
    if job is None or job.attempt != attempt or job.stage not in STEPS:
        return 2
    try:
        jobs.verify_inputs(job)
    except (ValueError, OSError) as exc:
        return _stop(jobs, job, "inputs_invalid", f"copied input is unusable: {exc}")
    try:
        ctx = _context(jobs, config, job)
        step = {"check": _check_step, "review": _review_step}.get(job.stage, _agent_step)
        return step(ctx)
    except (StaleAttempt, RpcAborted):
        return 2
    except InputsChanged as exc:
        return _stop(jobs, job, "inputs_invalid", str(exc))
    except (RpcError, StepError, OSError, ValueError, subprocess.SubprocessError) as exc:
        return _fail(jobs, job, str(exc) or type(exc).__name__)


def _cas(jobs: Jobs, job: JobState, stages, build: Callable[[JobState], dict]) -> bool:
    with jobs.state.lock():
        current = jobs.load().get(job.id)
        if current is None or current.attempt != job.attempt or current.stage not in stages:
            return False
        jobs.save(replace(current, **build(current)), _now())
        return True


def _apply(ctx: Ctx, build: Callable[[JobState], dict]) -> None:
    if not _cas(ctx.jobs, ctx.job, {ctx.stage}, build):
        raise StaleAttempt


def _event(job: JobState, **fields) -> list:
    return [*job.events, {"at": _now(), **fields}]


def _fail(jobs: Jobs, job: JobState, message: str) -> int:
    done = _cas(
        jobs,
        job,
        {job.stage},
        lambda j: {"error": message, "events": _event(j, type="step_failed", error=message)},
    )
    return 1 if done else 2


def _stop(jobs: Jobs, job: JobState, reason: str, message: str) -> int:
    done = _cas(
        jobs,
        job,
        {job.stage},
        lambda j: {
            "stage": "user_wait",
            "wait_reason": reason,
            "ended_at": _now(),
            "error": message,
            "events": _event(j, type="user_wait", reason=reason),
        },
    )
    return 1 if done else 2


# --- workspace and files -------------------------------------------------------------------


def _context(jobs: Jobs, config: JobConfig, job: JobState) -> Ctx:
    folder = jobs.folder(job)
    workspace = _workspace(job, folder, job_environment(config.env))
    ctx = Ctx(jobs, config, replace(job, workspace=str(workspace)), folder, workspace)
    hashes = _prepare(ctx)
    recorded = job.guideline_hashes
    if recorded is not None and recorded != hashes:
        raise InputsChanged("a copied review profile does not match its recorded hash")
    _apply(ctx, lambda j: {"workspace": str(workspace), "guideline_hashes": hashes})
    return ctx


def _workspace(job: JobState, folder: Path, env: Mapping[str, str]) -> Path:
    if job.review_target is not None:
        # Nothing is built, so the reviewer reads the target itself.
        return Path(job.target)
    if job.review != "code":
        path = folder / "work"
        path.mkdir(parents=True, exist_ok=True)
        return path
    path = folder / "worktree"
    if (path / ".git").exists():
        return path
    if path.exists() and any(path.iterdir()):
        raise StepError("the job worktree folder exists but is not a worktree")
    proc = subprocess.run(
        ["git", "-C", job.target, "worktree", "add", "--detach", str(path), "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        raise StepError(f"git worktree add failed: {proc.stderr.strip()}")
    return path


def _docs_source(folder: str = "review") -> Path:
    source = docs_dir(folder)
    if source is None:
        raise StepError(f"{folder} documents were not found")
    return source


def _layer(name: str) -> str:
    """The launch-path text of this path, read when the message is built."""
    try:
        return (_docs_source("launch") / name).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise StepError(f"launch layer is unreadable: {name}: {exc}") from exc


def _guidelines(job: JobState, folder: Path) -> tuple[Path, ...]:
    """The job's own copy of the review profile, so a basis cannot drift while it runs."""
    names = profile_names(job.review)
    target = folder / "review-docs"
    target.mkdir(parents=True, exist_ok=True)
    source = None
    for name in names:
        if not (target / name).is_file():
            source = source or _docs_source()
            shutil.copyfile(source / name, target / name)
    return tuple(target / name for name in names)


def _write_durable(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _prepare(ctx: Ctx) -> dict[str, str]:
    """Write the step's files durably; return the hashes of the job's review profile."""
    job, folder = ctx.job, ctx.folder
    (folder / "logs").mkdir(exist_ok=True)
    _write_durable(folder / "prompt.md", job.prompt)
    if job.review_target is None:
        _write_durable(folder / "WORKFLOW.md", _workflow(ctx))
    return {str(p): _file_sha(p) for p in _guidelines(job, folder)}


def _wait_command(ctx: Ctx) -> str:
    argv = [
        sys.executable,
        "-m",
        "taskwatch.job_cli",
        "--root",
        str(ctx.jobs.state.root),
        "wait",
        ctx.job.id,
        "--reason",
        "<what you need approved>",
    ]
    return " ".join(shlex.quote(part) for part in argv)


_APPROVAL_RULES = """## Approval
Approval source: {source}
Approved scope (complete; nothing outside it is approved):
{scope}

Do not write outside the workspace{exception}, send a message, call an external service or change
any external state beyond that scope. If a step needs something outside it, run this
command first, which records the request before you act, then stop work immediately and
take no further action:
  {command}
The user can then approve; the job resumes from its recorded stage."""


def _approval_text(ctx: Ctx) -> str:
    job = ctx.job
    # The job records sit in the job folder, so the write rule must name them as allowed.
    exception = (
        ""
        if job.review_target is not None
        else " (except this job's progress and checklist files, named in the workflow)"
    )
    return _APPROVAL_RULES.format(
        source=job.approval_source,
        scope=job.approval_scope,
        command=_wait_command(ctx),
        exception=exception,
    )


def _workflow(ctx: Ctx) -> str:
    job = ctx.job
    progress = ctx.folder / job.progress
    checklist = ctx.folder / job.checklist
    work_inputs = "\n".join(f"- {p}" for p in job.inputs.get("work", [])) or "- (none)"
    return f"""# Workflow for job {job.id} ({job.review})

Work only in the workspace: {ctx.workspace}
Exception: the progress and checklist files named below, which are in the job folder.

## Start
1. Read the intent: {job.intent}
2. Read the spec: {job.spec}
3. Read the original request: {ctx.folder / "prompt.md"}
4. Read the progress file `{progress}` if it exists, and continue unfinished work.
5. Read the checklist `{checklist}` if it exists.

Work inputs:
{work_inputs}

## While working
- Required result files, relative to the workspace: {", ".join(job.outputs)}
- Keep `{progress}` current: what is done, what remains, evidence.
- Keep `{checklist}` as JSON: {{"items": [{{"id": "...", "passes": true or false}}]}}.
  Set `passes` to true only when the evidence for that item exists.
- The progress and checklist files are outside the workspace; never copy them into it.
- Code jobs: commit everything in the workspace. The checks accept only a clean, committed
  result.
- If you delegate to subagents, wait until each one finished.

{_approval_text(ctx)}

## End
Finish only when the checklist is complete and the required files exist. The check command runs
outside your session: {json.dumps(job.check_command)}. A passing claim by you is not evidence.
"""


# --- messages ------------------------------------------------------------------------------


def _agent_message(ctx: Ctx, resumed: bool) -> str:
    job = ctx.job
    head = job.prompt
    lines = [
        "",
        "---",
        f"Taskwatch job {job.id} ({job.review}). Read {ctx.folder / 'WORKFLOW.md'} first.",
        f"Workspace: {ctx.workspace}",
        f"Intent: {job.intent}",
        f"Spec: {job.spec}",
    ]
    if job.review_target is None:
        lines += [f"Progress file: {ctx.folder / job.progress}"]
        lines += [f"Checklist file: {ctx.folder / job.checklist}"]
    for path in job.inputs.get("work", []):
        lines.append(f"Work input: {path}")
    lines += ["", _approval_text(ctx)]
    if ctx.stage == "fix":
        lines += ["", "This is a fix round. Fix exactly what follows, then finish."]
        lines.append(_fix_reason(job))
    if resumed:
        lines += [
            "",
            "You were interrupted. Read the progress file, finish only what is unfinished,",
            "and do not redo completed work.",
        ]
    return head + "\n".join(lines)


def _latest(job: JobState) -> dict | None:
    records = [r for r in (job.checks[-1:] + job.reviews[-1:]) if isinstance(r, Mapping)]
    return max(records, key=lambda r: r.get("seq", 0), default=None)


def _fix_reason(job: JobState) -> str:
    record = _latest(job)
    if record is None:
        return "No failure is recorded. Re-verify the result against the spec."
    if "exit_code" in record:
        problems = "\n".join(f"- {p}" for p in record["problems"])
        return f"The deterministic check failed.\n{problems}\nFull output: {record['log']}"
    problems = "\n".join(f"- {p}" for p in record["problems"])
    return f"The independent review did not pass.\n{problems}\n\nReview:\n{record['body']}"


def _subject_lines(ctx: Ctx) -> list[str]:
    job, target = ctx.job, ctx.job.review_target
    if target is None:
        log = job.checks[-1]["log"] if job.checks else ""
        return [
            f"Reviewed result: {ctx.workspace} (files: {', '.join(job.outputs)})",
            f"Result fingerprint: {job.fingerprint}",
            f"Check output: {log}",
        ]
    if "commit" in target:
        what = f"commit {target['commit']} of the repository at {ctx.workspace}"
    else:
        what = f"files {', '.join(target['files'])} under {ctx.workspace}"
    return [f"Review target: {what}", f"Target fingerprint: {job.fingerprint}"]


def _prior_lines(job: JobState, prior: list[Mapping]) -> list[str]:
    last = prior[-1]
    if job.review_target is not None:
        # Nothing was fixed between rounds: the last answer could not be accepted.
        problems = "; ".join(last["problems"]) or "none recorded"
        return [
            "",
            f"This is round {len(prior) + 1}. The last answer was not accepted: {problems}.",
            "Review the whole target again and answer in full.",
        ]
    lines = [
        "",
        f"This is re-review round {len(prior) + 1}. Review the changes since the last review",
        "and whether each finding below is resolved. Do not reopen settled ground.",
        f"Previously reviewed fingerprint: {last['fingerprint']}",
        f"Current fingerprint: {job.fingerprint}",
    ]
    if job.review == "code":
        lines.append(f"See: git diff {last['fingerprint']} {job.fingerprint}")
    return [*lines, "Findings of the last review:", last["body"]]


def _review_message(
    ctx: Ctx, guidelines: tuple[Path, ...], resumed: bool, review_root: Path
) -> str:
    job = ctx.job
    prior = [r for r in job.reviews if isinstance(r, Mapping)]
    lines = [
        f"You are the independent reviewer for job {job.id} ({job.review}).",
        f"Do not edit the deliverable. Write review evidence only under {review_root}.",
        f"Read these guidelines first: {', '.join(str(p) for p in guidelines)}",
        f"Intent: {job.intent}",
        f"Spec: {job.spec}",
        *_subject_lines(ctx),
        "If you delegate to subagents, wait until each one finished.",
        "",
        "Original request, in full:",
        job.prompt,
    ]
    if prior:
        lines += _prior_lines(job, prior)
    lines += [f"Review input: {p}" for p in job.inputs.get("review", [])]
    lines += ["", _layer("taskwatch.md")]
    if resumed:
        lines += ["", "You were interrupted. Review the current state again and answer in full."]
    return "\n".join(lines)


# --- RPC session ---------------------------------------------------------------------------


class Recorder:
    """Writes evidence from RPC frames. A refused write means the attempt is stale."""

    def __init__(self, ctx: Ctx) -> None:
        self.ctx = ctx
        self.abort = threading.Event()
        self.done = threading.Event()
        self._last = time.monotonic()

    def watch(self) -> None:
        """Raise the abort flag when the job leaves this attempt, even if omp is silent."""
        ctx = self.ctx
        while not self.done.wait(WATCH_EVERY):
            try:
                current = ctx.jobs.load().get(ctx.job.id)
            except (ValueError, OSError):
                continue
            if current is None or current.attempt != ctx.job.attempt or current.stage != ctx.stage:
                self.abort.set()
                return

    def frame(self, frame: dict) -> None:
        kind = frame.get("type")
        now = time.monotonic()
        if kind in _EVENTS:
            source = frame.get("payload") if isinstance(frame.get("payload"), dict) else frame
            summary = {k: source[k] for k in _EVENT_FIELDS if k in source}
            self._write({"type": kind, **summary})
        elif kind in _PROGRESS_FRAMES and now - self._last >= EVIDENCE_EVERY:
            self._write(None)
        else:
            return
        self._last = now

    def _write(self, event: dict | None) -> None:
        ctx = self.ctx

        def build(job: JobState) -> dict:
            now = _now()
            changes = {"evidence_at": now, **_progress_changes(job, ctx, now)}
            if event is not None:
                changes["events"] = _event(job, **event)
            return changes

        if not _cas(ctx.jobs, ctx.job, {ctx.stage}, build):
            self.abort.set()


def _command(ctx: Ctx) -> tuple[str, ...]:
    """The review step may use its own command; {guidelines} and {review} name the job copy."""
    config = ctx.config
    if ctx.stage != "review" or config.review_command is None:
        return config.omp_command
    fields = {"{guidelines}": str(ctx.folder / "review-docs"), "{review}": ctx.job.review}
    return tuple(fields.get(part, part) for part in config.review_command)


def _converse(ctx: Ctx, session_dir: Path, message: Callable[[bool], str], text: bool) -> str:
    timeout = float(ctx.config.rpc_timeout)
    recorder = Recorder(ctx)
    argv = [*_command(ctx), "--mode", "rpc", "--no-ui"]
    stderr = ctx.folder / "logs" / f"omp-a{ctx.job.attempt}.stderr"
    session_dir.mkdir(parents=True, exist_ok=True)
    watcher = threading.Thread(target=recorder.watch, daemon=True)
    watcher.start()
    try:
        with RpcClient(
            argv,
            ctx.workspace,
            on_frame=recorder.frame,
            stderr_path=stderr,
            abort=recorder.abort,
            poll=SETTLE_POLL,
            env=ctx.env,
        ) as client:
            client.start(timeout)
            opened = client.open_session(session_dir, timeout)
            resumed = opened.get("resumed") is True
            path = opened.get("sessionFile")
            _apply(
                ctx,
                lambda j: {
                    "session_path": path if isinstance(path, str) else "",
                    "session_dir": str(session_dir),
                    "events": _event(j, type="session_open", resumed=resumed),
                },
            )
            client.prompt_and_wait(message(resumed), timeout)
            reply = client.last_assistant_text(timeout) if text else ""
            client.finish()
    finally:
        recorder.done.set()
    if recorder.abort.is_set():
        raise StaleAttempt
    return reply


def _agent_step(ctx: Ctx) -> int:
    _converse(ctx, ctx.folder / "sessions" / "work", lambda r: _agent_message(ctx, r), False)
    stage = ctx.stage

    def build(job: JobState) -> dict:
        now = _now()
        return {
            "stage": "check",
            "resume_stage": "check",
            "ended_at": now,
            "error": "",
            "events": _event(job, type="step_exit", stage=stage, code=0),
            **_progress_changes(job, ctx, now),
        }

    _apply(ctx, build)
    return 0


def _inside(workspace: Path, relative: str) -> Path | None:
    path = (workspace / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(workspace.resolve()):
        return None
    return path


# --- progress, fingerprint, check ----------------------------------------------------------


def _progress_changes(job: JobState, ctx: Ctx, now: float) -> dict:
    current = progress_fingerprint(job, ctx.workspace, ctx.folder)
    if current == job.progress_hash:
        return {}
    return {"progress_hash": current, "last_progress_at": now}


def _fingerprint(job: JobState, workspace: Path, env: Mapping[str, str]) -> str:
    target = job.review_target
    if target is not None and "commit" in target:
        _git_ok(workspace, ["cat-file", "-e", f"{target['commit']}^{{commit}}"], env=env)
        return target["commit"]
    if job.review != "code" or target is not None:
        return files_fingerprint(workspace, job.outputs)
    dirty = uncommitted_changes(workspace, env)
    if dirty:
        raise FingerprintError(f"uncommitted changes: {'; '.join(dirty[:5])}")
    return head_sha(workspace, env)


def _basis(job: JobState, folder: Path) -> str:
    return basis_of(Path(job.intent), Path(job.spec), _guidelines(job, folder))


def _file_problems(job: JobState, workspace: Path, folder: Path) -> list[str]:
    problems = []
    for base, rel in [*((workspace, item) for item in job.outputs), (folder, job.progress)]:
        path = _inside(base, rel)
        if path is None or not path.is_file():
            problems.append(f"required file is missing: {rel}")
    path = _inside(folder, job.checklist)
    failing = checklist_problems(str(path)) if path is not None else ("missing",)
    if failing is None:
        problems.append("the checklist is not a nonempty JSON list of items with boolean passes")
    elif failing == ("missing",):
        problems.append(f"checklist is missing: {job.checklist}")
    elif failing:
        problems.append(f"checklist items do not pass: {', '.join(failing)}")
    return problems


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _run_command(ctx: Ctx, seq: int) -> tuple[dict, list[str]]:
    """Run the check argv in its own process group, and kill the whole group afterwards."""
    log = ctx.folder / "logs" / f"check-{seq}.log"
    code, problems = -1, []
    try:
        with log.open("wb") as out:
            proc = subprocess.Popen(
                ctx.job.check_command,
                cwd=ctx.workspace,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=ctx.env,
            )
            try:
                code = proc.wait(timeout=ctx.config.rpc_timeout)
                if code != 0:
                    problems.append(f"check command exited {code}")
            except subprocess.TimeoutExpired:
                problems.append("check command timed out")
            finally:
                _kill_group(proc.pid)
                proc.wait()
    except OSError as exc:
        log.write_text(f"{exc}\n", encoding="utf-8")
        problems.append(f"check command could not run: {exc}")
    record = {"exit_code": code, "log": str(log), "log_sha256": _file_sha(log)}
    return record, problems


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _check_step(ctx: Ctx) -> int:
    job = ctx.job
    seq = len(job.checks) + len(job.reviews) + 1
    run, problems = _run_command(ctx, seq)
    # After the command: it may have changed or removed what the checks require.
    problems += _file_problems(job, ctx.workspace, ctx.folder)
    fingerprint = basis = ""
    try:
        fingerprint = _fingerprint(job, ctx.workspace, ctx.env)
        basis = _basis(job, ctx.folder)
    except (ValueError, OSError) as exc:
        problems.append(str(exc))
    record = {
        "seq": seq,
        "at": _now(),
        "ok": not problems,
        **run,
        "problems": problems,
        "fingerprint": fingerprint,
        "basis": basis,
    }

    def build(current: dict) -> dict:
        changes = {
            "checks": [*current.checks, record],
            "fingerprint": fingerprint,
            "basis": basis,
            "events": _event(current, type="check", ok=not problems),
            **_progress_changes(current, ctx, _now()),
        }
        return {**changes, **_check_route(current, problems)}

    _apply(ctx, build)
    return 0


def _check_route(job: JobState, problems: list[str]) -> dict:
    ended = {"ended_at": _now()}
    if not problems:
        return {**ended, "stage": "review", "resume_stage": "review", "recoveries": 0, "error": ""}
    message = "; ".join(problems)
    if job.fix_rounds > 0:
        # A fix that follows a review is a review fix round, so a failing check spends one.
        if job.fix_rounds < MAX_FIX_ROUNDS:
            return {
                **ended,
                "stage": "fix",
                "resume_stage": "fix",
                "fix_rounds": job.fix_rounds + 1,
                "error": message,
            }
        return {**ended, "stage": "user_wait", "wait_reason": "review_exhausted", "error": message}
    if job.recoveries < MAX_RECOVERIES:
        return {
            **ended,
            "stage": "fix",
            "resume_stage": "fix",
            "recoveries": job.recoveries + 1,
            "error": message,
        }
    return {**ended, "stage": "user_wait", "wait_reason": "check_failed", "error": message}


# --- review --------------------------------------------------------------------------------


def _pin_subject(ctx: Ctx) -> Ctx:
    """A review-only job has no check to fix the result, so the review step does it."""
    try:
        fingerprint = _fingerprint(ctx.job, ctx.workspace, ctx.env)
        basis = _basis(ctx.job, ctx.folder)
    except (ValueError, OSError) as exc:
        raise StepError(f"the review target cannot be read: {exc}") from exc
    _apply(ctx, lambda j: {"fingerprint": fingerprint, "basis": basis})
    return replace(ctx, job=replace(ctx.job, fingerprint=fingerprint, basis=basis))


def _result_stale(ctx: Ctx) -> bool:
    job = ctx.job
    try:
        changed = _fingerprint(job, ctx.workspace, ctx.env) != job.fingerprint
        return changed or _basis(job, ctx.folder) != job.basis or not job.fingerprint
    except (ValueError, OSError):
        return True


def _review_step(ctx: Ctx) -> int:
    if ctx.job.review_target is not None:
        ctx = _pin_subject(ctx)
    elif _result_stale(ctx):
        _apply(ctx, lambda j: _requeue(j))
        return 0
    job = ctx.job
    guidelines = _guidelines(job, ctx.folder)
    name = f"review-{len(job.reviews)}-{len(job.checks)}"
    review_root = ctx.folder / "reviews" / name
    review_root.mkdir(parents=True, exist_ok=True)
    text = _converse(
        ctx,
        ctx.folder / "sessions" / name,
        lambda r: _review_message(ctx, guidelines, r, review_root),
        True,
    )
    try:
        ctx.jobs.verify_inputs(job)
    except (ValueError, OSError) as exc:
        raise InputsChanged(f"a copied input changed during the review: {exc}") from exc
    problems = _evidence_problems(ctx)
    if problems:
        # The reviewer saw evidence that no longer holds, so its verdict is not accepted.
        _apply(ctx, lambda j: {**_requeue(j), "error": "; ".join(problems)})
        return 0
    record = _judge(ctx, text)
    _apply(ctx, lambda j: _review_route(j, record))
    return 0


def _evidence_problems(ctx: Ctx) -> list[str]:
    """Recheck what the accepted check rested on; a PASS must not outlive it."""
    job = ctx.job
    if job.review_target is not None:
        return []
    check = job.checks[-1] if job.checks else None
    if (
        check is None
        or not check["ok"]
        or check["exit_code"] != 0
        or check["fingerprint"] != job.fingerprint
        or check["basis"] != job.basis
    ):
        return ["there is no accepted check for this result"]
    try:
        if _file_sha(Path(check["log"])) != check["log_sha256"]:
            return ["the check log does not match its recorded hash"]
    except OSError as exc:
        return [f"the check log is unreadable: {exc}"]
    return _file_problems(job, ctx.workspace, ctx.folder)


def _requeue(job: JobState) -> dict:
    """Send a result whose evidence went stale back to the step that produces the evidence."""
    first = "review" if job.review_target is not None else "check"
    changes = {
        "stage": first,
        "resume_stage": first,
        "fingerprint": "",
        "basis": "",
        "ended_at": _now(),
        "events": _event(job, type="review_stale"),
    }
    if job.recoveries >= MAX_RECOVERIES:
        return {**changes, "stage": "user_wait", "wait_reason": "result_unstable"}
    return {**changes, "recoveries": job.recoveries + 1}


def _judge(ctx: Ctx, body: str) -> dict:
    job = ctx.job
    seq = len(job.checks) + len(job.reviews) + 1
    record = {
        "seq": seq,
        "at": _now(),
        "round": len(job.reviews) + 1,
        "verdict": "",
        "blocking": True,
        "reviewer": "",
        "fingerprint": job.fingerprint,
        "outputs_fingerprint": "",
        "basis": job.basis,
        "body": body,
        "session_path": ctx.jobs.load()[job.id].session_path,
        "problems": [],
    }
    try:
        verdict, blocking, reviewer = _verdict_line(body)
        record.update(verdict=verdict, blocking=blocking, reviewer=reviewer)
    except ValueError as exc:
        record["problems"].append(str(exc))
    try:
        if _fingerprint(job, ctx.workspace, ctx.env) != job.fingerprint:
            record["problems"].append("the result changed during the review")
        record["outputs_fingerprint"] = files_fingerprint(ctx.workspace, job.outputs)
        if _basis(job, ctx.folder) != job.basis:
            record["problems"].append("the review basis changed during the review")
    except (ValueError, OSError) as exc:
        record["problems"].append(f"the result cannot be fingerprinted: {exc}")
    if record["verdict"] == "FAIL":
        record["problems"].append(_FAIL_PROBLEM)
    record["ok"] = not record["problems"] and record["verdict"] == "PASS" and not record["blocking"]
    # A review-only job has nothing to fix: a parsed verdict on an unchanged target is its result.
    record["final"] = (
        job.review_target is not None
        and record["verdict"] != ""
        and all(problem == _FAIL_PROBLEM for problem in record["problems"])
    )
    return record


def _review_route(job: JobState, record: dict) -> dict:
    changes = {
        "reviews": [*job.reviews, record],
        "ended_at": _now(),
        "events": _event(job, type="review", ok=record["ok"], verdict=record["verdict"]),
    }
    if record["ok"] or record["final"]:
        done = {"stage": "deliver_pending", "resume_stage": "deliver_pending", "error": ""}
        return {**changes, **done}
    message = "; ".join(record["problems"])
    retry = "review" if job.review_target is not None else "fix"
    if job.fix_rounds < MAX_FIX_ROUNDS:
        # Reserved here, before the retry runs: a failed attempt still used its round.
        return {
            **changes,
            "stage": retry,
            "resume_stage": retry,
            "fix_rounds": job.fix_rounds + 1,
            "error": message,
        }
    return {**changes, "stage": "user_wait", "wait_reason": "review_exhausted", "error": message}
