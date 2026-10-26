"""Conversational approvals are durable, idempotent, and bound to live results."""

import json
from datetime import datetime, timezone

import pytest

from taskwatch.config import config_path
from taskwatch.evidence import Run, append_run
from taskwatch.protocol import Card
from taskwatch.state import State
from taskwatch.tools import taskwatch_approve
from tests.config_fixture import write_config
from tests.fake_board import FakeBoard
from tests.git_fixture import git, make_repo
from tests.threads_fixture import in_threads

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


def test_start_retries_return_original_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    args = {"kind": "start", "approval_key": "apr-1", "approved": "start work", "message_id": "m1"}
    results = in_threads([lambda: json.loads(taskwatch_approve(args)) for _ in range(2)])
    assert results[0] == results[1]
    retry = json.loads(taskwatch_approve({**args, "approved": "different text"}))
    assert retry == results[0]
    record = State(tmp_path).load().approvals[0]
    assert record.message_id == "m1"
    assert record.approved == "start work"
    assert len(State(tmp_path).approvals_path.read_text().splitlines()) == 1


@pytest.mark.parametrize("change", ["none", "modified", "missing", "unknown"])
def test_apply_uses_live_fingerprint(tmp_path, monkeypatch, change):
    from taskwatch.fingerprint import files_fingerprint

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    write_config(config_path(tmp_path))
    result = tmp_path / "result.txt"
    result.write_text("approved result\n")
    fingerprint = files_fingerprint(tmp_path, ("result.txt",))
    state = State(tmp_path)
    append_run(state, Run("r1", "c1", "work", 0, str(tmp_path), False, ("result.txt",)), NOW)
    args = {
        "kind": "apply",
        "approved": "publish result",
        "message_id": "m2",
        "card_id": "c1",
        "fingerprint": fingerprint,
    }
    board = FakeBoard((Card(id="c1", status="review"),))
    if change == "modified":
        result.write_text("changed result\n")
    elif change == "missing":
        args["fingerprint"] = ""
    elif change == "unknown":
        args["card_id"] = "unknown"
    answer = json.loads(taskwatch_approve(args, writer=board))
    if change != "none":
        assert answer["ok"] is False
        assert state.load().approvals == ()
        return
    assert answer["ok"] is True
    assert answer["approval"]["approved"] == args["approved"]
    assert state.load().approvals[0].approved == args["approved"]
    assert json.loads(taskwatch_approve(args, writer=board)) == answer
    assert len(state.approvals_path.read_text().splitlines()) == 1
    assert state.load().approvals[0].fingerprint == fingerprint
    result.write_text("changed after approval\n")
    assert json.loads(taskwatch_approve(args, writer=board))["ok"] is False


def test_apply_refuses_dirty_git_at_same_head(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    write_config(config_path(tmp_path))
    repo = make_repo(tmp_path)
    head = git(repo, "rev-parse", "HEAD")
    state = State(tmp_path)
    append_run(state, Run("r1", "c1", "work", 0, str(repo), True, ("out.txt",)), NOW)
    (repo / "out.txt").write_text("unapproved change\n")
    assert git(repo, "rev-parse", "HEAD") == head
    answer = json.loads(
        taskwatch_approve(
            {
                "kind": "apply",
                "approved": "publish committed result",
                "message_id": "m-dirty",
                "card_id": "c1",
                "fingerprint": head,
            },
            writer=FakeBoard((Card(id="c1", status="review"),)),
        )
    )
    assert answer["ok"] is False
    assert state.load().approvals == ()
    assert not state.approvals_path.exists()


def test_approval_refuses_corrupt_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state = State(tmp_path)
    state.dir.mkdir(parents=True)
    state.approvals_path.write_text("not json\n")
    answer = json.loads(
        taskwatch_approve(
            {"kind": "start", "approval_key": "apr-1", "approved": "start work", "message_id": "m1"}
        )
    )
    assert answer["ok"] is False
    assert state.approvals_path.read_text() == "not json\n"
