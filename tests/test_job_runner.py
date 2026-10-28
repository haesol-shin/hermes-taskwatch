"""The runner against a fake omp RPC process: real subprocess, real files, no model."""

import base64
import json
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from taskwatch import job_runner
from taskwatch.fingerprint import files_fingerprint, head_sha
from taskwatch.job_rpc import FrameDecoder, RpcError
from taskwatch.job_runner import run_job
from taskwatch.jobs import JobConfig, Jobs, JobState
from tests.git_fixture import make_repo

FAKE = Path(__file__).with_name("fake_omp.py")
PASS = '{"verdict": "PASS", "blocking": false, "reviewer": "reviewer.invalid"}'
FAIL = '{"verdict": "FAIL", "blocking": true, "reviewer": "reviewer.invalid"}'
GOOD_FILES = {
    "out.txt": "ok\n",
    "PROGRESS.md": "done\n",
    "checklist.json": '{"items": [{"id": "a", "passes": true}]}',
}
CHECK = [
    sys.executable,
    "-c",
    "import sys; print('to-stdout', flush=True); print('to-stderr', file=sys.stderr);"
    " sys.exit(0 if open('out.txt').read().strip() == 'ok' else 1)",
]
GIT = [
    "git",
    "-c",
    "user.name=Dev",
    "-c",
    "user.email=dev@example.invalid",
    "-c",
    "commit.gpgsign=false",
]


class Env:
    def __init__(self, tmp_path: Path, monkeypatch, kind: str = "document", target=None):
        self.tmp = tmp_path
        self.jobs = Jobs(tmp_path / "root")
        self.config = JobConfig(omp_command=(sys.executable, str(FAKE)), rpc_timeout=5)
        self.state = tmp_path / "fake-state"
        monkeypatch.setenv("FAKE_OMP_SCENARIO", str(tmp_path / "scenario.json"))
        monkeypatch.setenv("FAKE_OMP_STATE", str(self.state))
        monkeypatch.setattr(job_runner, "SETTLE_POLL", 0.1)
        (tmp_path / "intent.md").write_text("intent text\n", encoding="utf-8")
        (tmp_path / "spec.md").write_text("spec text\n", encoding="utf-8")
        (tmp_path / "w.txt").write_text("work only\n", encoding="utf-8")
        (tmp_path / "r.txt").write_text("review only\n", encoding="utf-8")
        if target is None:
            target = tmp_path / "target"
            target.mkdir()
        self.job = self.jobs.submit(
            {
                "receipt_key": "k1",
                "approval_source": "msg-1",
                "approval_scope": "task-1",
                "prompt": "ORIGINAL PROMPT TEXT\nline two",
                "kind": kind,
                "target": str(target),
                "intent": str(tmp_path / "intent.md"),
                "spec": str(tmp_path / "spec.md"),
                "outputs": ["out.txt"],
                "check_command": CHECK,
                "checklist": "checklist.json",
                "progress": "PROGRESS.md",
                "inputs": {"work": [str(tmp_path / "w.txt")], "review": [str(tmp_path / "r.txt")]},
            },
            1000.0,
        )
        self.attempt = 0
        self.scenario([])

    def scenario(self, turns: list, **extra) -> None:
        # Each scenario starts at its first turn; the counter is shared across fake processes.
        (self.state / "turn.txt").unlink(missing_ok=True)
        (self.tmp / "scenario.json").write_text(
            json.dumps({"turns": turns, **extra}), encoding="utf-8"
        )

    def folder(self) -> Path:
        return self.jobs.folder(self.job)

    def get(self) -> JobState:
        return self.jobs.load()[self.job.id]

    def put(self, stage: str, **changes) -> JobState:
        self.attempt += 1
        with self.jobs.state.lock():
            job = self.get()
            job = replace(job, stage=stage, attempt=self.attempt, unit="u", ended_at=0.0, **changes)
            self.jobs.save(job, 2000.0 + self.attempt)
        return job

    def run(self, stage: str, **changes) -> int:
        self.put(stage, **changes)
        return run_job(self.jobs, self.config, self.job.id, self.attempt)

    def seed(self, files: dict, children: list | None = None) -> None:
        work = self.folder() / "work"
        work.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (work / name).write_text(text, encoding="utf-8")
        (self.folder() / "children-rpc.json").write_text(json.dumps(children or []))

    def log(self) -> list[dict]:
        return [json.loads(x) for x in (self.state / "log.jsonl").read_text().splitlines()]

    def prompts(self) -> list[str]:
        return [x["message"] for x in self.log() if x.get("type") == "prompt"]

    def to_review(self) -> None:
        self.seed(GOOD_FILES)
        assert self.run("check") == 0
        assert self.get().stage == "review"


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def work_turn(**extra) -> dict:
    return {"writes": GOOD_FILES, **extra}


# --- protocol -------------------------------------------------------------------------------


def chunk(chunk_id: str, index: int, count: int, length: int, data: bytes) -> dict:
    return {
        "type": "rpc_chunk",
        "chunkId": chunk_id,
        "index": index,
        "count": count,
        "byteLength": length,
        "data": base64.b64encode(data).decode(),
    }


def test_decoder_reassembles_and_rejects_bad_sequences():
    body = json.dumps({"type": "response", "id": "a"}).encode()
    half = len(body) // 2
    decoder = FrameDecoder()
    assert decoder.push(chunk("c", 0, 2, len(body), body[:half])) is None
    assert decoder.push(chunk("c", 1, 2, len(body), body[half:]))["id"] == "a"
    decoder = FrameDecoder()
    decoder.push(chunk("c", 0, 2, len(body), body[:half]))
    with pytest.raises(RpcError):
        decoder.push({"type": "agent_start"})
    decoder = FrameDecoder()
    decoder.push(chunk("c", 0, 2, len(body), body[:half]))
    with pytest.raises(RpcError):
        decoder.push(chunk("other", 1, 2, len(body), body[half:]))
    decoder = FrameDecoder()
    decoder.push(chunk("c", 0, 2, len(body), body[:half]))
    with pytest.raises(RpcError):
        decoder.push(chunk("c", 1, 2, len(body) + 3, body[half:]))
    with pytest.raises(RpcError):
        FrameDecoder(max_bytes=10).push(chunk("c", 0, 2, 11, b"x"))
    with pytest.raises(RpcError):
        FrameDecoder().push({**chunk("c", 0, 2, 4, b"abcd"), "data": "not base64!"})


# --- work and check -------------------------------------------------------------------------


def test_pipeline_from_work_to_deliver_pending(env):
    env.scenario([work_turn(), {"reply": f"{PASS}\nNo findings."}])
    assert env.run("work") == 0
    job = env.get()
    assert (job.stage, job.resume_stage) == ("check", "check")
    assert job.ended_at > 0 and job.workspace == str(env.folder() / "work")
    assert job.session_path.endswith("s1.jsonl")
    assert job.session_dir == str(env.folder() / "sessions" / "work")
    types = [e["type"] for e in job.events]
    assert {"agent_start", "prompt_result", "session_settled", "step_exit"} <= set(types)
    log = env.log()
    assert {"type": "negotiate_protocol", "id": log[1]["id"], "protocolVersion": 2} == log[1]
    assert log[0]["argv"] == ["--mode", "rpc", "--no-ui"]
    assert log[0]["cwd"] == str(env.folder() / "work")
    assert env.prompts()[0].startswith("ORIGINAL PROMPT TEXT\nline two")
    assert "msg-1" in env.prompts()[0] and "task-1" in env.prompts()[0]
    assert f"--root {env.jobs.state.root} wait {job.id}" in env.prompts()[0]
    assert "task-1" in (env.folder() / "WORKFLOW.md").read_text()
    assert (env.folder() / "WORKFLOW.md").is_file()
    assert (env.folder() / "review-docs" / "document.md").exists() is False
    assert (env.folder() / "review-docs" / "source-checked-document.md").is_file()

    assert env.run("check") == 0
    job = env.get()
    assert job.stage == "review" and job.recoveries == 0
    record = job.checks[-1]
    assert record["ok"] and record["exit_code"] == 0
    assert Path(record["log"]).read_text().split() == ["to-stdout", "to-stderr"]
    assert job.fingerprint == files_fingerprint(env.folder() / "work", ["out.txt"])
    assert job.basis

    assert env.run("review") == 0
    job = env.get()
    assert job.stage == "deliver_pending"
    review = job.reviews[-1]
    assert review["ok"] and review["fingerprint"] == job.fingerprint
    assert "No findings." in review["body"] and len(review["body_sha256"]) == 64
    assert Path(job.session_dir).name == "review-0-1"
    assert "common.md" in env.prompts()[-1] and job.fingerprint in env.prompts()[-1]
    work_prompt, review_prompt = env.prompts()
    assert "work-0.txt" in work_prompt and "review-0.txt" not in work_prompt
    assert "review-0.txt" in review_prompt and "work-0.txt" not in review_prompt
    assert "prepared" in [e["type"] for e in job.events] and job.guideline_hashes


def test_stale_attempt_runs_nothing(env):
    env.put("work")
    assert run_job(env.jobs, env.config, env.job.id, env.attempt - 1) == 2
    assert not (env.state / "log.jsonl").exists()
    assert run_job(env.jobs, env.config, "0" * 32, 1) == 2


def test_check_failures_route_to_fix_then_user_wait(env):
    env.seed({**GOOD_FILES, "out.txt": "bad\n"})
    assert env.run("check") == 0
    job = env.get()
    assert (job.stage, job.recoveries, job.ended_at > 0) == ("fix", 1, True)
    assert "exited 1" in job.error and "to-stderr" in Path(job.checks[-1]["log"]).read_text()
    assert job.fingerprint == job.checks[-1]["fingerprint"]
    assert env.run("check", recoveries=3) == 0
    job = env.get()
    assert (job.stage, job.wait_reason) == ("user_wait", "check_failed")


def test_check_requires_files_checklist_and_progress(env):
    env.seed({"out.txt": "ok\n", "checklist.json": '{"items": [{"id": "a", "passes": false}]}'})
    assert env.run("check") == 0
    problems = env.get().checks[-1]["problems"]
    assert any("PROGRESS.md" in p for p in problems)
    assert any("do not pass: a" in p for p in problems)


def test_prompt_result_alone_is_not_acceptance(env):
    env.config = JobConfig(omp_command=env.config.omp_command, rpc_timeout=1)
    env.scenario([work_turn(no_settle=True)])
    assert env.run("work") == 1
    job = env.get()
    assert job.stage == "work" and "timed out" in job.error and job.ended_at == 0
    env.scenario([work_turn(no_agent_start=True)])
    assert env.run("work") == 1
    assert "agent_start" in env.get().error


def test_dead_process_leaves_stage_and_resume_opens_same_session(env):
    env.scenario([{"die_after_content": True}, work_turn()])
    assert env.run("work") == 1
    job = env.get()
    assert job.stage == "work" and "exited" in job.error and job.session_path
    assert env.run("work", session_dir=job.session_dir) == 0
    assert env.get().stage == "check"
    opens = [x for x in env.log() if x.get("type") == "open_session"]
    assert opens[0]["sessionDir"] == opens[1]["sessionDir"]
    assert "interrupted" in env.prompts()[1] and "interrupted" not in env.prompts()[0]
    assert len(Path(job.session_path).read_text().splitlines()) == 2
    assert [x["resumed"] for x in env.get().events if x["type"] == "session_open"] == [
        False,
        True,
    ]


def test_empty_session_is_not_resumed(env):
    env.scenario([{"die": True}, work_turn()])
    assert env.run("work") == 1
    job = env.get()
    assert Path(job.session_path).stat().st_size == 0
    assert env.run("work", session_dir=job.session_dir) == 0
    assert [x["resumed"] for x in env.get().events if x["type"] == "session_open"] == [
        False,
        False,
    ]
    assert all("interrupted" not in p for p in env.prompts())


def test_cancelled_attempt_exits_promptly_and_writes_nothing(env, monkeypatch):
    monkeypatch.setattr(job_runner, "WATCH_EVERY", 0.1)
    env.config = JobConfig(omp_command=env.config.omp_command, rpc_timeout=30)
    env.scenario([work_turn(sleep=6)])
    env.put("work")
    result = []
    started = time.monotonic()
    thread = threading.Thread(
        target=lambda: result.append(run_job(env.jobs, env.config, env.job.id, env.attempt))
    )
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (env.state / "log.jsonl").exists():
        time.sleep(0.05)
    time.sleep(0.5)
    with env.jobs.state.lock():
        env.jobs.save(replace(env.get(), stage="cancelled"), 3000.0)
    thread.join(5)
    assert result == [2] and time.monotonic() - started < 5
    job = env.get()
    assert job.stage == "cancelled" and "step_exit" not in [e["type"] for e in job.events]


# --- review ---------------------------------------------------------------------------------


def test_review_failure_counts_a_round_and_failed_fix_keeps_it(env):
    env.scenario([{"reply": f"{FAIL}\nP1: wrong"}, {"die": True}])
    env.to_review()
    assert env.run("review") == 0
    job = env.get()
    assert (job.stage, job.fix_rounds, job.ended_at > 0) == ("fix", 1, True)
    assert not job.reviews[-1]["ok"] and "P1: wrong" in job.reviews[-1]["body"]
    assert env.run("fix") == 1
    job = env.get()
    assert (job.stage, job.fix_rounds) == ("fix", 1)
    assert "P1: wrong" in env.prompts()[-1]


def test_review_stops_after_three_fix_rounds(env):
    env.scenario([{"reply": f"{FAIL}\nstill wrong"}])
    env.to_review()
    assert env.run("review", fix_rounds=3) == 0
    job = env.get()
    assert (job.stage, job.wait_reason) == ("user_wait", "review_exhausted")
    assert job.fix_rounds == 3


def test_incomplete_review_output_cannot_pass(env):
    env.scenario([{"reply": "looks fine to me"}])
    env.to_review()
    assert env.run("review") == 0
    job = env.get()
    assert (job.stage, job.fix_rounds) == ("fix", 1)
    assert "exactly one verdict line" in job.error


def test_result_changed_during_review_rejects_pass(env):
    env.scenario([{"writes": {"out.txt": "ok\nchanged\n"}, "reply": PASS}])
    env.to_review()
    assert env.run("review") == 0
    job = env.get()
    assert job.stage == "fix" and not job.reviews[-1]["ok"]
    assert "changed during the review" in job.error


@pytest.mark.parametrize("case", ["log", "checklist", "children_cache", "review_input"])
def test_review_pass_needs_its_evidence_to_still_hold(env, case):
    env.to_review()
    job = env.get()
    writes = {}
    if case == "log":
        Path(job.checks[-1]["log"]).write_text("MODEL CLAIM: PASS\n", encoding="utf-8")
    elif case == "checklist":
        (env.folder() / "work" / "checklist.json").unlink()
    elif case == "children_cache":
        (env.folder() / "children-rpc.json").write_text("not json", encoding="utf-8")
    else:
        writes[job.inputs["review"][0]] = "CHANGED REVIEW CRITERIA"
    env.scenario([{"reply": PASS, "writes": writes}])
    assert env.run("review") == (1 if case == "review_input" else 0)
    current = env.get()
    assert current.reviews == ()
    if case == "review_input":
        assert (current.stage, current.wait_reason) == ("user_wait", "inputs_invalid")
    else:
        assert current.stage == "check" and current.fingerprint == ""
        assert "review_stale" in [e["type"] for e in current.events]


def test_result_changed_before_review_goes_back_to_check(env):
    env.to_review()
    (env.folder() / "work" / "out.txt").write_text("ok\nlater\n", encoding="utf-8")
    assert env.run("review") == 0
    job = env.get()
    assert (job.stage, job.fingerprint) == ("check", "")
    assert not (env.state / "log.jsonl").exists()


# --- children -------------------------------------------------------------------------------


def test_nonzero_exit_or_persistence_notice_fails_a_settled_step(env):
    env.scenario([work_turn()], exit_code=1)
    assert env.run("work") == 1
    job = env.get()
    assert job.stage == "work" and "exited with code 1" in job.error
    env.scenario([work_turn(notice=True)])
    assert env.run("work") == 1
    assert "session-persistence" in env.get().error


def test_oversize_frame_is_rejected(env):
    env.scenario([work_turn(oversize=True)])
    assert env.run("work") == 1
    assert "frame limit" in env.get().error


def test_check_judges_files_after_the_command_and_kills_strays(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    stray = env.folder() / "work" / "stray.txt"
    script = (
        "import os, subprocess, sys\n"
        "os.remove('checklist.json')\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(1.5);"
        ' open("stray.txt", "w").write("x")\'])\n'
    )
    env.seed(GOOD_FILES)
    with env.jobs.state.lock():
        env.jobs.save(replace(env.get(), check_command=(sys.executable, "-c", script)), 1500.0)
    assert env.run("check") == 0
    job = env.get()
    assert job.stage == "fix" and "checklist is missing" in job.error
    time.sleep(2.2)
    assert not stray.exists()


def child(ident: str, status: str, count: int = 5) -> dict:
    messages = [{"n": i, "text": "x" * 400} for i in range(count)]
    return {"id": ident, "status": status, "messages": messages}


def test_running_child_is_not_accepted(env):
    env.scenario([work_turn(subagents=[child("Kid", "running")])])
    assert env.run("work") == 1
    assert "not completed" in env.get().error and env.get().stage == "work"


def test_manifest_entry_without_outputs_is_rejected(env):
    manifest = json.dumps([{"id": "Kid", "outputs": []}])
    env.scenario(
        [
            work_turn(
                subagents=[child("Kid", "completed")],
                writes={**GOOD_FILES, str(env.folder() / "children.json"): manifest},
            )
        ]
    )
    assert env.run("work") == 1
    assert "lists no output" in env.get().error


def test_completed_child_without_transcript_parks_the_job(env):
    manifest = json.dumps([{"id": "Kid", "outputs": ["kid.txt"]}])
    kid = {"id": "Kid", "status": "completed", "messages": []}
    files = {**GOOD_FILES, "kid.txt": "result", str(env.folder() / "children.json"): manifest}
    env.scenario([{"writes": files, "subagents": [kid]}])
    assert env.run("work") == 1
    job = env.get()
    assert (job.stage, job.wait_reason) == ("user_wait", "child_evidence_unavailable")


def test_review_running_child_is_rejected(env):
    env.scenario([{"reply": PASS, "subagents": [child("R", "running")]}])
    env.to_review()
    assert env.run("review") == 1 and "not completed" in env.get().error


def test_rereview_sees_prior_findings_and_original_request(env):
    env.scenario(
        [
            {"reply": f"{FAIL}\nP1: old finding"},
            work_turn(),
            {"reply": f"{PASS}\nok"},
        ]
    )
    env.to_review()
    assert env.run("review") == 0 and env.get().stage == "fix"
    assert env.run("fix") == 0 and env.run("check") == 0
    assert env.run("review") == 0
    prompt = env.prompts()[-1]
    assert "re-review round 2" in prompt and "P1: old finding" in prompt
    assert "ORIGINAL PROMPT TEXT" in prompt and env.get().stage == "deliver_pending"


def test_review_children_need_their_own_manifest_and_outputs(env):
    root = env.folder() / "reviews" / "review-0-1"
    manifest = json.dumps([{"id": "R", "outputs": ["r.txt"]}])
    worker_manifest = env.folder() / "children.json"
    worker_manifest.write_text(manifest, encoding="utf-8")  # a stale worker manifest is not used
    env.scenario(
        [
            {"reply": PASS, "subagents": [child("R", "completed")]},
            {
                "reply": PASS,
                "subagents": [child("R", "completed")],
                "writes": {str(root / "children.json"): manifest, str(root / "r.txt"): "x"},
            },
        ]
    )
    env.to_review()
    assert env.run("review") == 1 and "review-0-1" not in env.get().error
    assert "children.json" in env.get().error
    assert env.run("review") == 0 and env.get().stage == "deliver_pending"


def test_async_followup_failure_after_prompt_result_fails_the_step(env):
    env.scenario([work_turn(followup_error="error")])
    assert env.run("work") == 1
    job = env.get()
    assert job.stage == "work" and "stopReason error" in job.error


def test_terminal_children_pruned_from_registry_are_still_read(env):
    env.scenario([work_turn(subagents=[child("Gone", "completed"), child("Live", "running")])])
    assert env.run("work") == 1
    saved = json.loads((env.folder() / "children-rpc.json").read_text())
    assert {c["id"]: c["status"] for c in saved} == {"Gone": "completed", "Live": "running"}
    assert all(len(c["messages"]) == 5 for c in saved)
    events = [e for e in env.get().events if e["type"] == "subagent_lifecycle"]
    assert {(e["id"], e["status"]) for e in events} >= {("Gone", "started"), ("Gone", "completed")}


def test_child_seen_before_death_is_kept_or_blocks_resume(env):
    manifest = json.dumps([{"id": "Kid", "outputs": ["kid.txt"]}])
    resume = work_turn(
        writes={**GOOD_FILES, "kid.txt": "r\n", str(env.folder() / "children.json"): manifest}
    )
    env.scenario([{"subagents": [child("Kid", "completed")], "die_after_children": "read"}, resume])
    assert env.run("work") == 1
    assert env.run("work", session_dir=env.get().session_dir) == 0
    saved = json.loads((env.folder() / "children-rpc.json").read_text())
    assert [c["id"] for c in saved] == ["Kid"] and len(saved[0]["messages"]) == 5
    assert env.get().stage == "check"


def test_unrecoverable_earlier_child_blocks_instead_of_passing_empty(env):
    # The child was announced, but the process died before its transcript could be saved.
    dying = {"subagents": [child("Kid", "completed")], "die_after_children": "now"}
    env.scenario([dying, work_turn()])
    assert env.run("work") == 1
    assert env.run("work", session_dir=env.get().session_dir) == 1
    job = env.get()
    assert (job.stage, job.wait_reason) == ("user_wait", "child_evidence_unavailable")


def test_corrupt_child_evidence_blocks(env):
    env.scenario([work_turn(subagents=[child("Kid", "completed")])])
    env.run("work")
    path = env.folder() / "evidence" / "work.json"
    path.write_text(path.read_text().replace("xxxx", "yyyy"), encoding="utf-8")
    assert env.run("work", session_dir=env.get().session_dir) == 1
    assert env.get().wait_reason == "child_evidence_unavailable"


def test_child_status_tampering_with_same_messages_blocks(env):
    env.scenario([work_turn(subagents=[child("Kid", "failed")])])
    env.run("work")
    path = env.folder() / "evidence" / "work.json"
    path.write_text(path.read_text().replace('"failed"', '"completed"'), encoding="utf-8")
    assert env.run("work", session_dir=env.get().session_dir) == 1
    assert env.get().wait_reason == "child_evidence_unavailable"


def test_failed_check_after_review_spends_a_fix_round(env):
    env.seed({**GOOD_FILES, "out.txt": "bad\n"})
    assert env.run("check", fix_rounds=1) == 0
    job = env.get()
    assert (job.stage, job.fix_rounds, job.recoveries) == ("fix", 2, 0)
    assert env.run("check", fix_rounds=3) == 0
    job = env.get()
    assert (job.stage, job.wait_reason) == ("user_wait", "review_exhausted")
    assert env.run("check", recoveries=3, fix_rounds=0) == 0
    assert env.get().wait_reason == "check_failed"


def test_children_need_a_manifest(env):
    env.scenario([work_turn(subagents=[child("Kid", "completed")])])
    assert env.run("work") == 1
    assert "children.json" in env.get().error


def test_children_full_transcript_over_chunked_frames_and_manifest(env):
    manifest = [{"id": "Kid", "outputs": ["kid.txt"]}]
    env.scenario(
        [
            work_turn(
                subagents=[child("Kid", "completed")],
                writes={
                    **GOOD_FILES,
                    "kid.txt": "r\n",
                    str(env.folder() / "children.json"): json.dumps(manifest),
                },
            )
        ],
        chunk_at=500,
    )
    assert env.run("work") == 0
    saved = json.loads((env.folder() / "children-rpc.json").read_text())
    assert [m["n"] for m in saved[0]["messages"]] == [0, 1, 2, 3, 4]
    assert env.run("check") == 0 and env.get().stage == "review"
    (env.folder() / "work" / "kid.txt").unlink()
    assert env.run("check") == 0
    assert "output is missing" in env.get().error


# --- code jobs ------------------------------------------------------------------------------


def test_code_job_uses_worktree_and_committed_fingerprint(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "repo")
    env = Env(tmp_path, monkeypatch, kind="code", target=repo)
    env.scenario([work_turn(run=[["git", "add", "-A"], [*GIT, "commit", "-m", "work"]])])
    assert env.run("work") == 0
    worktree = env.folder() / "worktree"
    assert env.get().workspace == str(worktree) and (worktree / ".git").exists()
    assert env.run("check") == 0
    job = env.get()
    assert job.stage == "review" and job.fingerprint == head_sha(worktree)
    assert (env.folder() / "review-docs" / "code.md").is_file()
    (worktree / "out.txt").write_text("ok\nmore\n", encoding="utf-8")
    assert env.run("check") == 0
    job = env.get()
    assert job.stage == "fix" and "uncommitted" in job.error
    subprocess.run(["git", "-C", str(repo), "worktree", "list"], check=True, capture_output=True)


def test_missing_input_hash_match_stops_job(env):
    Path(next(iter(env.get().input_hashes))).write_text("tampered", encoding="utf-8")
    assert env.run("work") == 1
    job = env.get()
    assert (job.stage, job.wait_reason) == ("user_wait", "inputs_invalid")
    assert not (env.state / "log.jsonl").exists()


def test_changed_review_profile_stops_job(env):
    env.scenario([work_turn()])
    assert env.run("work") == 0
    profile = env.folder() / "review-docs" / "common.md"
    profile.write_text(profile.read_text() + "\nedited\n", encoding="utf-8")
    assert env.run("check") == 1
    job = env.get()
    assert (job.stage, job.wait_reason) == ("user_wait", "inputs_invalid")
