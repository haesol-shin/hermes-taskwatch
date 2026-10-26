"""Build and run an executor argument list. No shell.

Every run writes a run record: where the result lives and how the run ended. The gate
and the scan read that record and hash the result themselves. A review run also writes
the review record, and its verdict, blocking flag, and reviewer come from the review
output the executor wrote. The exit code only says the executor ran.

A review output holds exactly one line that is a JSON object:
{"verdict": "PASS" or "FAIL", "blocking": true or false, "reviewer": "<name>"}.
"""

import json
import os
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .config import Config
from .evidence import Run, append_run, basis_of, result_fingerprint
from .fingerprint import FingerprintError, head_sha, uncommitted_changes
from .state import RECORD_VERSION, Review, State

_PLACEHOLDERS = ("{brief}", "{copy}", "{output}")


def run_executor(
    config: Config,
    kind: str,
    card: str,
    brief: str,
    copy: str,
    output: str,
    now: datetime,
    state: State,
    runner: Callable[[list[str], Path], int],
    *,
    results: tuple[str, ...] = (),
    git: bool = False,
    sha: str | None = None,
    intent: Path | None = None,
    spec: Path | None = None,
    guidelines: tuple[Path, ...] = (),
    checklist: str | None = None,
    progress: str | None = None,
) -> dict:
    """Run the template. ``copy`` is what the executor is given and where the result lives.

    Every path is made absolute here, once. The run record and the checks after it use the
    same paths whatever the working directory of the process that reads them.
    """
    if kind not in {"work", "review"}:
        return _block("unknown kind")
    if not git and not results:
        return _block("name at least one required result file")
    copy, output = _absolute(copy), _absolute(output)
    checklist = _absolute(checklist) if checklist else None
    progress = _absolute(progress) if progress else None
    guidelines = tuple(Path(_absolute(item)) for item in guidelines)
    try:
        argv = _argv(config, kind, brief, copy, output)
        if kind == "review":
            _check_reviewed_copy(Path(copy), git, sha, intent, spec)
    except (ValueError, FingerprintError) as exc:
        return _block(str(exc))
    run_id = uuid.uuid4().hex
    if kind == "review":
        # A body from an earlier run must not pass as this run's review.
        _set_aside(Path(output), run_id)
    log = state.dir / "runs" / f"{run_id}.log"
    code = runner(argv, log)
    missing = [item for item in results if not (Path(copy) / item).is_file()]
    run = Run(
        run_id,
        card,
        kind,
        code,
        copy,
        git,
        results,
        checklist,
        progress,
        tuple(str(item) for item in guidelines),
        str(log),
    )
    append_run(state, run, now)
    context = {"run_id": run_id, "exit_code": code, "log": str(log)}
    if code != 0:
        return _block(f"exit code is {code}", **context)
    if missing:
        return _block(f"missing required output: {', '.join(missing)}", **context)
    done = {"ok": True, "outputs": list(results), **context}
    if kind == "work":
        return done
    try:
        verdict = _record_review(state, card, run, output, sha, intent, spec, guidelines, now)
    except (ValueError, FingerprintError, OSError) as exc:
        return _block(str(exc), **context)
    return {**done, "verdict": verdict}


def _absolute(path: str) -> str:
    return os.path.abspath(path)


def _set_aside(path: Path, run_id: str) -> None:
    if path.exists():
        path.rename(path.with_name(f"{path.name}.before-{run_id}"))


def _argv(config: Config, kind: str, brief: str, copy: str, output: str) -> list[str]:
    template = config.executors.work if kind == "work" else config.executors.review
    values = {"{brief}": brief, "{copy}": copy, "{output}": output}
    argv = []
    for part in template:
        if any(mark in part and part != mark for mark in _PLACEHOLDERS):
            raise ValueError("placeholder must be its own argument")
        argv.append(values.get(part, part))
    return argv


def _check_reviewed_copy(
    copy: Path, git: bool, sha: str | None, intent: Path | None, spec: Path | None
) -> None:
    if intent is None or spec is None:
        raise ValueError("a review needs the intent and the spec")
    if not git:
        return
    if sha is None:
        raise ValueError("a Git review needs the reviewed sha")
    if uncommitted_changes(copy):
        raise ValueError("uncommitted changes")
    head = head_sha(copy)
    if head != sha:
        raise ValueError(f"the copy is at {head[:12]}, not the reviewed sha {sha[:12]}")


def _record_review(state, card, run, output, sha, intent, spec, guidelines, now) -> str:
    # The earlier body was moved aside before the run, so a file here is this run's.
    if not Path(output).is_file():
        raise ValueError("the review run wrote no output")
    body = Path(output).read_text(encoding="utf-8")
    verdict, blocking, reviewer = _verdict_line(body)
    copy = Path(run.copy)
    fingerprint, dirty = result_fingerprint(copy, run.git, run.results)
    if run.git and (dirty or fingerprint != sha):
        raise ValueError("the copy changed during the review")
    basis = basis_of(intent, spec, guidelines)
    with state.lock():
        loaded = state.load()
        if loaded.error:
            raise ValueError(loaded.error)
        latest = max((item.round for item in loaded.reviews if item.card == card), default=0)
        review = Review(
            RECORD_VERSION, latest + 1, card, fingerprint, basis, reviewer, verdict, blocking
        )
        state.append_review(review, body, now)
    return verdict


def _verdict_line(body: str) -> tuple[str, bool, str]:
    found = []
    for line in body.splitlines():
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            obj = json.loads(text)
        except ValueError:
            continue
        if isinstance(obj, dict) and "verdict" in obj:
            found.append(obj)
    if len(found) != 1:
        raise ValueError("the review output needs exactly one verdict line")
    obj = found[0]
    verdict, blocking, reviewer = obj.get("verdict"), obj.get("blocking"), obj.get("reviewer")
    if verdict not in {"PASS", "FAIL"} or not isinstance(blocking, bool):
        raise ValueError("the verdict line needs verdict PASS or FAIL and a boolean blocking")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("the verdict line needs a reviewer")
    if verdict == "PASS" and blocking:
        raise ValueError("a PASS cannot carry a blocking finding")
    return verdict, blocking, reviewer.strip()


def _block(reason: str, **extra) -> dict:
    return {"ok": False, "block": reason, **extra}
