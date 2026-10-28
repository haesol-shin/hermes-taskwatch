"""Standalone job commands. This module does not load the Hermes adapter."""

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

from .jobs import JobConfig, Jobs, load_job_config
from .state import RecordError, _create_new, _make_dirs


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="taskwatch")
    cli.add_argument("--root", type=Path, default=None)
    commands = cli.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Write config and timer units without activating them")
    init.add_argument("--unit-dir", type=Path)
    submit = commands.add_parser("submit", help="Durably store an approved job JSON document")
    submit.add_argument("file", type=Path)
    commands.add_parser("scan", help="Schedule job steps once")
    run = commands.add_parser("run", help="Run one reserved job step")
    run.add_argument("job")
    run.add_argument("--attempt", type=int, required=True)
    show = commands.add_parser("show")
    show.add_argument("job", nargs="?")
    for name in ("cancel", "wait", "approve", "resume"):
        action = commands.add_parser(name)
        action.add_argument("job")
        action.add_argument("--reason", required=True)
        if name == "approve":
            action.add_argument("--scope", help="Replace the approved scope with this exact text")
    return cli


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = (
        args.root or Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    ).resolve()
    try:
        result = _dispatch(args, root)
    except (ValueError, OSError, RecordError, KeyError) as exc:
        print(f"taskwatch: {exc}", file=sys.stderr)
        return 1
    if result is not None:
        print(json.dumps(result, sort_keys=True))
    return 0


def _dispatch(args: argparse.Namespace, root: Path):
    jobs = Jobs(root)
    if args.command == "init":
        return _init(root, args.unit_dir)
    if args.command == "submit":
        return jobs.submit(json.loads(args.file.read_text(encoding="utf-8"))).to_json()
    if args.command == "show":
        records = jobs.load()
        return records[args.job].to_json() if args.job else [j.to_json() for j in records.values()]
    if args.command == "scan":
        from .job_scan import scan_jobs

        return scan_jobs(jobs, load_job_config(root))
    if args.command == "run":
        from .job_runner import run_job

        code = run_job(jobs, load_job_config(root), args.job, args.attempt)
        if code:
            raise ValueError(f"job step failed with exit code {code}")
        return None
    return _action(jobs, args)


def _action(jobs: Jobs, args: argparse.Namespace) -> dict:
    now = time.time()
    with jobs.state.lock():
        job = jobs.load()[args.job]
        command = args.command
        if command == "cancel":
            job = replace(job, stage="cancelled", wait_reason=args.reason)
        elif command == "wait":
            if job.stage not in {"work", "check", "review", "fix", "queued"}:
                raise ValueError("only an unfinished job can wait for approval")
            resume = job.resume_stage if job.stage == "queued" else job.stage
            job = replace(job, resume_stage=resume, stage="approval_wait", wait_reason=args.reason)
        elif command == "approve":
            if job.stage != "approval_wait":
                raise ValueError("job is not waiting for approval")
            job = replace(
                job,
                approval_required=False,
                approval_source=args.reason,
                approval_scope=args.scope if args.scope is not None else job.approval_scope,
                stage="queued",
                wait_reason="",
            )
        elif command == "resume":
            if job.stage != "user_wait":
                raise ValueError("job needs user judgment")
            # A resume into a review fix opens a new cycle and spends its first round now, so
            # the cap still bounds fixes; a check repair keeps the separate recovery budget.
            reviewing = job.resume_stage == "fix" and job.fix_rounds > 0
            job = replace(
                job,
                stage="deliver_pending" if job.resume_stage == "deliver_pending" else "queued",
                wait_reason="",
                error="",
                recoveries=0,
                fix_rounds=int(reviewing),
                delivery_failures=0,
            )
        job = replace(
            job, events=(*job.events, {"type": command, "at": now, "reason": args.reason})
        )
        jobs.save(job, now)
        return job.to_json()


def _init(root: Path, unit_dir: Path | None) -> dict:
    directory = root / "taskwatch"
    _make_dirs(directory)
    config = directory / "jobs-config.json"
    data = {"version": 1, **asdict(JobConfig())}
    if not config.exists():
        _create_new(config, (json.dumps(data, indent=2) + "\n").encode(), 0o600)
    load_job_config(root)
    if unit_dir is not None:
        from .units import timer_text

        _make_dirs(unit_dir)
        command = " ".join(
            _unit_arg(item)
            for item in (sys.executable, "-m", "taskwatch.job_cli", "--root", str(root), "scan")
        )
        service = (
            "[Unit]\nDescription=Scan the taskwatch job board once\n\n"
            "[Service]\nType=oneshot\n" + f"ExecStart={command}\n"
        )
        for name, text in (
            ("hermes-taskwatch-scan.service", service),
            ("hermes-taskwatch-scan.timer", timer_text()),
        ):
            path = unit_dir / name
            if path.exists():
                raise ValueError("unit exists; inspect it before replacing the timer entry point")
            _create_new(path, text.encode(), 0o644)
    return {"config": str(config), "units_written": unit_dir is not None, "activated": False}


def _unit_arg(value: str) -> str:
    if any(char in value for char in "\n\r\x00"):
        raise ValueError("unit arguments cannot contain control characters")
    return (
        '"'
        + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        + '"'
    )


if __name__ == "__main__":
    raise SystemExit(main())
