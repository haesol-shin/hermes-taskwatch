"""Passes against the installed Hermes package. Skipped when it cannot be imported.

The flow goes through the real parts: the worker script writes the run record, the gate
hook reads it, the tools change the card, and the next card's run gets the approved
snapshot. Nothing here edits a card body to make a check pass.
"""

import argparse
import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from taskwatch.card_body import loads
from taskwatch.commands import draft_config
from taskwatch.config import config_path, load_config
from taskwatch.evidence import EvidenceBoard
from taskwatch.plugin import register
from taskwatch.state import State

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("hermes_cli") is None,
    reason="Hermes is not importable",
)
NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]
STUB = Path(__file__).resolve().parent / "executor_stub.py"
SCRIPT = ROOT / "examples/worker/run_executor.py"
SENT: list[tuple[str, str]] = []
PASS = '{"verdict": "PASS", "blocking": false, "reviewer": "reviewer.invalid"}'
FAIL = '{"verdict": "FAIL", "blocking": true, "reviewer": "reviewer.invalid"}'


class _Ctx:
    profile_name = "main"

    def register_cli_command(self, **kwargs):
        del kwargs

    def register_hook(self, name, callback):
        del name, callback

    def register_tool(self, **kwargs):
        del kwargs


def _isolate(root: Path) -> None:
    resolved = root.resolve()
    if os.path.commonpath([str(resolved), "/tmp"]) != "/tmp":
        raise RuntimeError("the temporary Hermes home must stay under /tmp")
    os.environ["HERMES_HOME"] = str(resolved)
    for name in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_TASK",
    ):
        os.environ.pop(name, None)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _send_platform() -> None:
    """A platform the real send path accepts, so a delivery can succeed without a network."""
    from gateway.platform_registry import PlatformEntry, platform_registry

    def handler(args, chat_id, platform_name, pconfig):
        SENT.append((chat_id, args["message"]))
        return {"success": True, "message_id": "1"}

    platform_registry.register(
        PlatformEntry(
            name="twtest",
            label="taskwatch test",
            adapter_factory=lambda config: None,
            check_fn=lambda: True,
            send_message_handler=handler,
            parse_target_ref_fn=lambda ref: (ref, None),
            source="plugin",
        )
    )


def _start(root: Path, board: str = "twaccept", user_platform: str = "twtest", **times):
    root.mkdir(parents=True, exist_ok=True)
    _isolate(root)
    from hermes_cli.kanban_db import create_board

    register(_Ctx())
    _send_platform()
    SENT.clear()
    _write(
        root / "profiles" / "main" / "config.yaml", "plugins:\n  enabled:\n    - hermes-taskwatch\n"
    )
    _write(root / "config.yaml", "platforms:\n  twtest:\n    enabled: true\n")
    create_board(board)
    _write_config(root, board, user_platform, **times)
    return load_config(config_path(root))


def _write_config(root: Path, board: str, user_platform: str, **times) -> None:
    raw = draft_config(str(root.resolve()), board, ("main",))
    template = [sys.executable, str(STUB), "--brief", "{brief}", "--copy", "{copy}"]
    template += ["--output", "{output}"]
    raw["executors"] = {"work": template, "review": template}
    raw["destinations"]["user_notify"] = json.dumps(
        {"platform": user_platform, "chat_id": "chat1"}, separators=(",", ":")
    )
    raw["times"].update(times)
    _write(config_path(root), json.dumps(raw, indent=2) + "\n")


def _conn(board: str = "twaccept"):
    from hermes_cli.kanban_db import kanban_db_path
    from hermes_cli.kanban_db_connect import connect_closing

    return connect_closing(kanban_db_path(board))


def _task(card_id: str, board: str = "twaccept"):
    from hermes_cli.kanban_db import get_task

    with _conn(board) as conn:
        return get_task(conn, card_id)


def _by_title(title: str, board: str = "twaccept") -> str:
    from hermes_cli.kanban_db import list_tasks

    with _conn(board) as conn:
        return next(task.id for task in list_tasks(conn) if task.title == title)


def _parser():
    from taskwatch.cli import setup_cli

    parser = argparse.ArgumentParser()
    setup_cli(parser)
    return parser


def _worker(root: Path, card: str, kind: str, work: Path, plan: dict, *extra: str) -> dict:
    """Run the example worker script as a child process, as a Kanban worker would."""
    briefs = root / "briefs"
    briefs.mkdir(exist_ok=True)
    brief = briefs / f"brief-{len(list(briefs.iterdir()))}.json"
    _write(brief, json.dumps(plan))
    done = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--kind",
            kind,
            "--card",
            card,
            "--brief",
            str(brief),
            "--copy",
            str(work),
            "--output",
            str(briefs / f"out-{kind}.md"),
            *extra,
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=root,
    )
    lines = done.stdout.splitlines()
    assert len(lines) == 1, (done.stdout, done.stderr)
    return json.loads(lines[0])


def _tmp_root(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))


def test_live_worker_gate_next_card_flow(capsys):
    root = _tmp_root("tw-flow-")
    try:
        _flow(root, capsys)
    finally:
        shutil.rmtree(root)


def _flow(root: Path, capsys) -> None:
    from hermes_cli.kanban_db import (
        complete_task,
        create_board,
        create_task,
        kanban_db_path,
        request_review,
    )

    from taskwatch.cli import handle_cli
    from taskwatch.gate import pre_tool_call
    from taskwatch.hermes_board import open_board
    from taskwatch.tools import taskwatch_create, taskwatch_reject

    config = _start(root)
    state = State(root)
    state.append_start("apr-1", NOW, "start the work")
    parser = _parser()
    with pytest.raises(SystemExit) as caught:
        handle_cli(parser.parse_args(["doctor", "--root", str(root)]))
    doctor = capsys.readouterr().out
    assert caught.value.code == 1
    assert "board: ok" in doctor
    assert "plugin:main: ok" in doctor
    assert "hermes: ok" in doctor
    assert "reject: fail" in doctor

    with _conn() as conn:
        plain = create_task(conn, title="watched", body="plain", initial_status="blocked")
    handle_cli(parser.parse_args(["scan", "--root", str(root)]))
    assert _task(plain).status == "blocked"
    assert pre_tool_call("kanban_complete", {"task_id": plain, "board": "twaccept"})["action"] == (
        "block"
    )

    # A call that Hermes sends to another database is not ours, whatever its arguments name.
    create_board("other")
    with _conn("other") as conn:
        foreign = create_task(conn, title="elsewhere", body="plain")
    os.environ["HERMES_KANBAN_DB"] = str(kanban_db_path("other"))
    try:
        assert pre_tool_call("kanban_complete", {"task_id": foreign, "board": "twaccept"}) is None
    finally:
        os.environ.pop("HERMES_KANBAN_DB")

    intent, spec = root / "intent.md", root / "spec.md"
    _write(intent, "## Source\nuser\n## Purpose\nwhy\n## Done\nwhen\n## Approval\napr-1\n")
    _write(
        spec,
        "## Outputs\nout\n## Checklist\nlist.json\n## Out of scope\nnone\n"
        "## Live\npath.invalid\n## Guidance\nnone\n## Model\nexample\n",
    )
    created = json.loads(
        taskwatch_create(
            {
                "approval_key": "apr-1",
                "intent": str(intent),
                "spec": str(spec),
                "cards": [{"title": "gate work", "gate": True}, {"title": "next", "parents": [0]}],
            }
        )
    )
    assert created["ok"] is True
    gate, following = created["card_id"], _by_title("next")
    work = root / "work"
    work.mkdir()
    checklist = root / "checklist.json"
    _write(checklist, json.dumps({"items": [{"id": "item-1", "passes": True}]}))
    out = work / "out.txt"
    request = {"task_id": gate, "board": "twaccept"}
    approve = {"task_id": gate, "board": "twaccept"}

    # Nothing ran yet, so the gate has no exit code to accept.
    assert "exit code" in pre_tool_call("kanban_request_review", request)["message"]

    def work_run(text: str) -> dict:
        plan = {"write": {str(out): text}, "print": "noise"}
        return _worker(
            root, gate, "work", work, plan, "--result", "out.txt", "--checklist", str(checklist)
        )

    def review_run(line: str) -> dict:
        plan = {"output": f"# Review\n{line}\n"}
        return _worker(root, gate, "review", work, plan, "--result", "out.txt")

    first = work_run("v1\n")
    assert first["ok"] is True
    assert pre_tool_call("kanban_request_review", request) is None
    with _conn() as conn:
        assert request_review(conn, gate, summary="ready for the gate")
    assert review_run(FAIL)["verdict"] == "FAIL"
    assert "no PASS" in pre_tool_call("kanban_complete", approve)["message"]

    rejected = json.loads(
        taskwatch_reject({"card_id": gate, "reason": "fix it", "reject_id": "r1"})
    )
    assert rejected["opened"] is True
    assert _task(gate).status != "review"
    reader = EvidenceBoard(open_board("twaccept"), state)
    assert reader.card(gate).reject_count == 1

    assert work_run("v2\n")["ok"] is True
    assert pre_tool_call("kanban_request_review", request) is None
    with _conn() as conn:
        assert request_review(conn, gate, summary="ready again")
    # A new reject right after the first one, in the same second, reopens the card again.
    # The first reject's reopen event is not this one's.
    again = json.loads(taskwatch_reject({"card_id": gate, "reason": "more", "reject_id": "r2"}))
    assert (again["opened"], again["round"]) == (True, 2)
    assert _task(gate).status != "review"
    assert work_run("v2\n")["ok"] is True
    assert pre_tool_call("kanban_request_review", request) is None
    with _conn() as conn:
        assert request_review(conn, gate, summary="ready once more")
    assert review_run(PASS)["verdict"] == "PASS"
    assert [item.round for item in state.load().reviews] == [1, 2]

    # The result changes after the PASS: the gate refuses until it is the reviewed one again.
    out.write_text("UNREVIEWED\n", encoding="utf-8")
    assert "no PASS" in pre_tool_call("kanban_complete", approve)["message"]
    out.write_text("v2\n", encoding="utf-8")
    assert pre_tool_call("kanban_complete", approve) is None
    snapshot = Path(loads(_task(gate).body)["snapshot_path"])
    assert (snapshot / "out.txt").read_text(encoding="utf-8") == "v2\n"
    with _conn() as conn:
        assert complete_task(conn, gate, summary="approved")

    assert handle_cli(parser.parse_args(["check", gate, "--root", str(root)])).startswith("pass")
    out.write_text("v3 after the approval\n", encoding="utf-8")

    # The next card gets its own copy made from the frozen snapshot, not the work copy that
    # changed since. The executor writes into {copy}, and that copy is what is checked.
    assert _task(following).status == "ready"
    seen = root / "seen-copy"
    follow_work = root / "follow"
    plan = {"echo_copy": str(seen), "write_in_copy": {"result.txt": "next\n"}}
    ran = _worker(root, following, "work", follow_work, plan, "--result", "result.txt")
    assert ran["ok"] is True
    assert seen.read_text(encoding="utf-8") == str(follow_work)
    assert (follow_work / "out.txt").read_text(encoding="utf-8") == "v2\n"
    assert (follow_work / "result.txt").read_text(encoding="utf-8") == "next\n"
    assert sorted(path.name for path in snapshot.iterdir()) == ["out.txt"]
    assert (snapshot / "out.txt").read_text(encoding="utf-8") == "v2\n"
    assert config.board == "twaccept"


def test_live_review_gate_reject_reopens_the_fix_worker_in_one_step(monkeypatch):
    root = _tmp_root("tw-review-")
    try:
        _review_gate(root, monkeypatch)
    finally:
        shutil.rmtree(root)


def _review_gate(root: Path, monkeypatch) -> None:
    from hermes_cli import kanban_db as kb

    from taskwatch.hermes_board import nested_reopen_available
    from taskwatch.tools import taskwatch_create, taskwatch_reject

    _start(root)
    state = State(root)
    state.append_start("apr-1", NOW, "start the work")
    intent, spec = root / "intent.md", root / "spec.md"
    _write(intent, "## Source\nuser\n## Purpose\nwhy\n## Done\nwhen\n## Approval\napr-1\n")
    _write(
        spec,
        "## Outputs\nout\n## Checklist\nlist.json\n## Out of scope\nnone\n"
        "## Live\npath.invalid\n## Guidance\nnone\n## Model\nexample\n",
    )
    cards = [
        {"title": "fix"},
        {"title": "review", "gate": True, "kind": "review", "parents": [0]},
        {"title": "after", "parents": [1]},
    ]
    created = json.loads(
        taskwatch_create(
            {"approval_key": "apr-1", "intent": str(intent), "spec": str(spec), "cards": cards}
        )
    )
    fixer, reviewer, after = created["card_id"], _by_title("review"), _by_title("after")
    with _conn() as conn:
        assert kb.complete_task(conn, fixer, summary="fixed")
    assert _task(reviewer).status == "ready"
    with _conn() as conn:
        assert kb.request_review(conn, reviewer, summary="reviewed")

    assert nested_reopen_available() is False
    refused = json.loads(taskwatch_reject({"card_id": reviewer, "reason": "no", "reject_id": "r1"}))
    assert refused["error"] == "core update needed"
    assert [_task(item).status for item in (fixer, reviewer, after)] == ["done", "review", "todo"]

    # The upstream change: reopen_review_task nests inside a caller's transaction.
    source = inspect.getsource(kb.reopen_review_task).replace(
        "write_txn(conn)", "write_txn(conn, allow_nested=True)"
    )
    patched = root / "nested_reopen.py"
    _write(patched, source)
    namespace = dict(vars(kb))
    exec(compile(source, str(patched), "exec"), namespace)
    monkeypatch.setattr(kb, "reopen_review_task", namespace["reopen_review_task"])
    assert nested_reopen_available() is True

    args = {"card_id": reviewer, "reason": "wrong result", "reject_id": "r2"}
    cards = (fixer, reviewer, after)

    def trace() -> list:
        with _conn() as conn:
            return [
                (
                    kb.get_task(conn, item).status,
                    len(kb.list_comments(conn, item)),
                    len(kb.list_events(conn, item)),
                )
                for item in cards
            ]

    # A failure after the first writes of the composite step leaves the board as it was:
    # no status, comment, or event survives. Without one transaction they would.
    before = trace()
    real_invalidate = kb.invalidate_descendants_for_parent_reopen

    def broken(*args, **kwargs):
        raise OSError("injected failure while invalidating the descendants")

    monkeypatch.setattr(kb, "invalidate_descendants_for_parent_reopen", broken)
    failed = json.loads(taskwatch_reject(args))
    assert failed["ok"] is False
    assert failed["retry"] is True
    assert trace() == before
    monkeypatch.setattr(kb, "invalidate_descendants_for_parent_reopen", real_invalidate)

    opened = json.loads(taskwatch_reject(args))
    assert opened["ok"] is True
    # The fix worker runs again, and nothing behind it can run on the old result.
    assert [_task(item).status for item in cards] == ["ready", "todo", "todo"]
    after_reject = trace()
    assert json.loads(taskwatch_reject(args)) == opened
    assert trace() == after_reject
    with _conn() as conn:
        reopened = [e for e in kb.list_events(conn, reviewer) if e.kind == "review_reopened"]
    assert len(reopened) == 1


def test_live_worker_gate_reject_retries_after_the_comment_and_a_failed_reopen(monkeypatch):
    root = _tmp_root("tw-retry-")
    try:
        _worker_gate_retry(root, monkeypatch)
    finally:
        shutil.rmtree(root)


def _worker_gate_retry(root: Path, monkeypatch) -> None:
    from hermes_cli import kanban_db as kb

    from taskwatch.evidence import REJECTS
    from taskwatch.tools import taskwatch_create, taskwatch_reject

    _start(root)
    state = State(root)
    state.append_start("apr-1", NOW, "start the work")
    intent, spec = root / "intent.md", root / "spec.md"
    _write(intent, "## Source\nuser\n## Purpose\nwhy\n## Done\nwhen\n## Approval\napr-1\n")
    _write(
        spec,
        "## Outputs\nout\n## Checklist\nlist.json\n## Out of scope\nnone\n"
        "## Live\npath.invalid\n## Guidance\nnone\n## Model\nexample\n",
    )
    created = json.loads(
        taskwatch_create(
            {
                "approval_key": "apr-1",
                "intent": str(intent),
                "spec": str(spec),
                "cards": [{"title": "gate", "gate": True}],
            }
        )
    )
    gate = created["card_id"]
    with _conn() as conn:
        assert kb.claim_task(conn, gate)
        assert kb.request_review(conn, gate, summary="done")

    # The comment lands, then the first reopen fails. Hermes records the comment as an event
    # too, so only a review_reopened event may tell the retry that the card already went back.
    real_reopen = kb.reopen_review_task
    failures = []

    def flaky(conn, card_id, *args, **kwargs):
        if not failures:
            failures.append(card_id)
            return False
        return real_reopen(conn, card_id, *args, **kwargs)

    monkeypatch.setattr(kb, "reopen_review_task", flaky)
    args = {"card_id": gate, "reason": "wrong result", "reject_id": "r1"}
    failed = json.loads(taskwatch_reject(args))
    assert failed["ok"] is False
    assert failed["retry"] is True
    assert _task(gate).status == "review"

    opened = json.loads(taskwatch_reject(args))
    assert opened["ok"] is True
    assert opened["round"] == 1
    assert _task(gate).status == "ready"
    with _conn() as conn:
        kinds = [event.kind for event in kb.list_events(conn, gate)]
        comments = [item.body for item in kb.list_comments(conn, gate)]
    assert kinds.count("review_reopened") == 1
    assert comments == ["reject r1: wrong result"]
    records = [item for item in state.read_records(REJECTS) if item["reject_id"] == "r1"]
    assert [item["phase"] for item in records] == ["started", "done"]
    assert records[1]["result"] == opened


def test_live_scan_warns_once_and_reaches_the_user_through_hermes(capsys):
    root = _tmp_root("tw-scan-")
    try:
        _scan_flow(root, capsys)
    finally:
        shutil.rmtree(root)


def _scan_flow(root: Path, capsys) -> None:
    from hermes_cli.kanban_db import create_task, list_tasks

    from taskwatch.cli import handle_cli

    _start(root, rewarn_initial_seconds=1, user_notify_unreceived_seconds=1)
    parser = _parser()
    with _conn() as conn:
        blocked = create_task(conn, title="stuck", body="plain", initial_status="blocked")

    def scan() -> str:
        handle_cli(parser.parse_args(["scan", "--root", str(root)]))
        return capsys.readouterr().out

    def warnings() -> list[str]:
        with _conn() as conn:
            return [task.title for task in list_tasks(conn) if task.id != blocked]

    scan()
    time.sleep(1.2)
    scan()
    time.sleep(1.2)
    # The user line is tried on this scan, though no new internal warning is due.
    _write_config(
        root, "twaccept", "nowhere", rewarn_initial_seconds=1, user_notify_unreceived_seconds=1
    )
    failed = scan()
    assert f"undelivered {blocked}:blocked" in failed
    assert [item.channel for item in State(root).load().warnings].count("user") == 0
    assert SENT == []
    _write_config(
        root, "twaccept", "twtest", rewarn_initial_seconds=1, user_notify_unreceived_seconds=1
    )
    delivered = scan()
    assert f"{blocked}:blocked" in delivered
    assert SENT[0][0] == "chat1"
    assert f"{blocked}:blocked" in SENT[0][1]
    assert [item.channel for item in State(root).load().warnings].count("user") == 1
    assert scan() == ""
    # Only the one cause was ever warned about: the warning cards are not checked as work.
    assert set(warnings()) == {f"{blocked}:blocked"}
