"""Delivery of finished jobs, driven through scans and the CLI with a real fake sender process."""

import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from taskwatch import job_delivery
from taskwatch.job_cli import main as cli_main
from taskwatch.job_scan import scan_jobs
from taskwatch.jobs import Jobs, JobState, load_job_config
from tests.test_job_scan import Board

SENDER = r"""
import json, os, signal, sys, time
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
with open(args[args.index("--file") + 1], encoding="utf-8") as handle:
    text = handle.read()
seen = ""
root = os.environ.get("FAKE_JOBS_FILE", "")
if root:
    with open(root, encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    attempts = rows[-1]["job"]["deliveries"]
    seen = attempts[-1]["status"] if attempts else ""
calls = os.path.join(here, "calls.jsonl")
with open(calls, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"args": args, "text": text, "seen": seen}) + "\n")
    handle.flush()
    index = sum(1 for _ in open(calls, encoding="utf-8")) - 1
with open(os.path.join(here, "modes.json"), encoding="utf-8") as handle:
    modes = json.load(handle)
mode = modes[min(index, len(modes) - 1)]
ok = {"success": True, "message_id": "msg-1"}
if mode == "success":
    print(json.dumps(ok))
elif mode == "slow_success":
    time.sleep(0.5)
    print(json.dumps(ok))
elif mode == "number_id":
    print(json.dumps({"success": True, "message_id": 42}))
elif mode == "failure":
    print(json.dumps({"success": False, "error": "refused"}))
    sys.exit(1)
elif mode == "skipped":
    print(json.dumps({"skipped": True, "reason": "duplicate"}))
elif mode == "no_message_id":
    print(json.dumps({"success": True}))
elif mode == "success_false_exit_zero":
    print(json.dumps({"success": False}))
elif mode == "exit_one_success_true":
    print(json.dumps(ok))
    sys.exit(1)
elif mode == "malformed":
    print("not json at all")
elif mode == "array":
    print("[1, 2]")
elif mode == "unreadable":
    sys.stdout.buffer.write(b"\xff\xfe\x00{")
    sys.stdout.flush()
elif mode == "nonjson_failure":
    print("boom", file=sys.stderr)
    sys.exit(2)
elif mode == "killed":
    os.kill(os.getpid(), signal.SIGKILL)
elif mode == "hang":
    time.sleep(30)
"""

PROMPT = "Write the synthetic report\nSecond line must not be the title"
REVIEW_BODY = "Looks fine.\n" + json.dumps({"verdict": "PASS", "blocking": False, "reviewer": "r"})
UNKNOWN_MODES = [
    "malformed",
    "array",
    "unreadable",
    "killed",
    "skipped",
    "no_message_id",
    "success_false_exit_zero",
    "exit_one_success_true",
    "nonjson_failure",
]


class Sender:
    def __init__(self, directory: Path) -> None:
        directory.mkdir()
        self.dir = directory
        (directory / "sender.py").write_text(SENDER, encoding="utf-8")
        self.command = (
            sys.executable,
            str(directory / "sender.py"),
            "--to",
            "person.invalid",
            "--file",
            "{message}",
            "--json",
        )

    def modes(self, *modes: str) -> None:
        (self.dir / "modes.json").write_text(json.dumps(list(modes)), encoding="utf-8")

    def calls(self) -> list[dict]:
        path = self.dir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture(autouse=True)
def _short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job_delivery, "DELIVERY_TIMEOUT", 5)


@pytest.fixture
def sender(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sender:
    fake = Sender(tmp_path / "sender")
    fake.modes("success")
    jobs_file = tmp_path / "home" / "taskwatch" / "state" / "jobs.jsonl"
    monkeypatch.setenv("FAKE_JOBS_FILE", str(jobs_file))
    return fake


@pytest.fixture
def board(tmp_path: Path, sender: Sender) -> Board:
    result = Board(tmp_path)
    result.config = replace(result.config, deliver_command=sender.command)
    return result


def finished_job(board: Board, **changes: object) -> JobState:
    """A job whose review passed and whose step service is already released."""
    job_id = board.add(prompt=PROMPT)
    folder = board.jobs.folder(board.get(job_id))
    workspace = folder / "work"
    workspace.mkdir(parents=True)
    (workspace / "out.txt").write_text("result", encoding="utf-8")
    log = folder / "logs" / "check-1.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("all green", encoding="utf-8")
    board.set(
        job_id,
        stage="deliver_pending",
        resume_stage="deliver_pending",
        workspace=str(workspace),
        ended_at=board.clock,
        checks=({"seq": 1, "ok": True, "exit_code": 0, "log": str(log)},),
        reviews=({"seq": 2, "ok": True, "verdict": "PASS", "body": REVIEW_BODY},),
        **changes,
    )
    return board.get(job_id)


def statuses(job: JobState) -> list[str]:
    return [attempt["status"] for attempt in job.deliveries]


def cli(board: Board, action: str, job_id: str, reason: str = "operator") -> None:
    assert cli_main(["--root", str(board.root / "home"), action, job_id, "--reason", reason]) == 0


def test_success_marks_done_and_records_the_provider_message_id(board: Board, sender: Sender):
    job = finished_job(board)
    board.scan()
    done = board.get(job.id)
    assert done.stage == "done"
    assert statuses(done) == ["success"]
    attempt = done.deliveries[0]
    assert attempt["exit_code"] == 0
    assert attempt["message_id"] == "msg-1"
    assert len(sender.calls()) == 1
    # The destination is the literal argument of the configured command, not the workspace.
    args = sender.calls()[0]["args"]
    assert args[:2] == ["--to", "person.invalid"]
    assert args[-1] == "--json"
    assert job.target not in args


def test_numeric_message_id_counts_as_a_receipt(board: Board, sender: Sender):
    sender.modes("number_id")
    job = finished_job(board)
    board.scan()
    done = board.get(job.id)
    assert done.stage == "done"
    assert done.deliveries[0]["message_id"] == 42


def test_the_message_is_built_from_the_record_and_hashed_before_sending(
    board: Board, sender: Sender
):
    job = finished_job(board)
    board.scan()
    folder = board.jobs.folder(job)
    call = sender.calls()[0]
    message_path = Path(call["args"][call["args"].index("--file") + 1])
    assert message_path == folder / "delivery.txt"
    text = message_path.read_text(encoding="utf-8")
    assert text == call["text"]
    lines = text.splitlines()
    assert PROMPT.splitlines()[0] in lines[0]
    assert "Second line must not be the title" not in lines[0]
    assert job.id in text
    assert "PASS" in text
    workspace = Path(job.workspace if job.workspace else folder / "work")
    assert str(folder / "work" / "out.txt") in text
    assert str(workspace) in text or str(folder / "work") in text
    assert str(folder / "logs" / "check-1.log") in text
    review_file = folder / "delivery-review.txt"
    assert str(review_file) in text
    assert review_file.read_text(encoding="utf-8") == REVIEW_BODY
    attempt = board.get(job.id).deliveries[0]
    assert attempt["message_file"] == str(message_path)
    assert attempt["message_sha256"] == hashlib.sha256(message_path.read_bytes()).hexdigest()


def test_attempt_is_durable_as_started_before_the_sender_runs(board: Board, sender: Sender):
    job = finished_job(board)
    board.scan()
    assert sender.calls()[0]["seen"] == "started"
    assert statuses(board.get(job.id)) == ["success"]


def test_definite_failure_retries_on_the_next_scan_and_the_third_stops(
    board: Board, sender: Sender
):
    sender.modes("failure")
    job = finished_job(board)
    board.scan()
    after_one = board.get(job.id)
    assert (after_one.stage, after_one.delivery_failures) == ("deliver_pending", 1)
    assert after_one.deliveries[0]["status"] == "failure"
    assert after_one.deliveries[0]["exit_code"] == 1
    assert "refused" in after_one.deliveries[0]["stdout"]
    board.scan()
    assert board.get(job.id).delivery_failures == 2
    assert board.get(job.id).stage == "deliver_pending"
    board.scan()
    stopped = board.get(job.id)
    assert (stopped.stage, stopped.wait_reason) == ("user_wait", "delivery_failed")
    assert stopped.delivery_failures == 3
    assert stopped.resume_stage == "deliver_pending"
    assert statuses(stopped) == ["failure"] * 3
    board.scan()
    board.scan()
    assert len(sender.calls()) == 3
    assert board.get(job.id) == stopped


def test_a_success_after_a_failure_finishes_the_job_and_clears_the_count(
    board: Board, sender: Sender
):
    sender.modes("failure", "success")
    job = finished_job(board)
    board.scan()
    assert board.get(job.id).delivery_failures == 1
    board.scan()
    done = board.get(job.id)
    assert done.stage == "done"
    assert done.delivery_failures == 0
    assert statuses(done) == ["failure", "success"]


def test_timeout_is_unknown_and_never_resent(
    board: Board, sender: Sender, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(job_delivery, "DELIVERY_TIMEOUT", 0.5)
    sender.modes("hang")
    job = finished_job(board)
    board.scan()
    parked = board.get(job.id)
    assert (parked.stage, parked.wait_reason) == ("user_wait", "delivery_unknown")
    assert statuses(parked) == ["unknown"]
    assert parked.delivery_failures == 0
    board.scan()
    board.scan()
    assert len(sender.calls()) == 1
    assert board.get(job.id).stage == "user_wait"


@pytest.mark.parametrize("mode", UNKNOWN_MODES)
def test_an_unprovable_outcome_is_unknown_and_never_resent(board: Board, sender: Sender, mode: str):
    sender.modes(mode, "success")
    job = finished_job(board)
    board.scan()
    parked = board.get(job.id)
    assert (parked.stage, parked.wait_reason) == ("user_wait", "delivery_unknown")
    assert statuses(parked) == ["unknown"]
    assert parked.delivery_failures == 0
    board.scan()
    board.scan()
    assert len(sender.calls()) == 1
    assert board.get(job.id).stage == "user_wait"


def test_an_interrupted_started_attempt_is_unknown_and_not_resent(board: Board, sender: Sender):
    attempt = {
        "at": 1.0,
        "command": list(sender.command),
        "message_file": "gone",
        "message_sha256": "0" * 64,
        "status": "started",
    }
    job = finished_job(board, deliveries=(attempt,))
    board.scan()
    parked = board.get(job.id)
    assert (parked.stage, parked.wait_reason) == ("user_wait", "delivery_unknown")
    assert sender.calls() == []
    board.scan()
    assert sender.calls() == []
    cli(board, "resume", job.id)
    board.scan()
    done = board.get(job.id)
    assert done.stage == "done"
    assert statuses(done) == ["unknown", "success"]
    assert len(sender.calls()) == 1


def test_a_running_unit_defers_delivery_until_it_is_released(board: Board, sender: Sender):
    job = finished_job(board, unit="taskwatch-old.service")
    board.service.states["taskwatch-old.service"] = "active"
    board.service.stop_ok = False
    board.scan()
    assert sender.calls() == []
    assert board.get(job.id).stage == "deliver_pending"
    board.kill(job.id)
    board.service.stop_ok = True
    board.scan()
    assert board.get(job.id).stage == "done"
    assert len(sender.calls()) == 1


def test_resume_after_unknown_sends_once_without_rerunning_work(board: Board, sender: Sender):
    sender.modes("malformed", "success")
    job = finished_job(board)
    board.scan()
    assert board.get(job.id).wait_reason == "delivery_unknown"
    cli(board, "resume", job.id)
    resumed = board.get(job.id)
    assert resumed.stage == "deliver_pending"
    assert resumed.delivery_failures == 0
    assert board.scan() == []
    done = board.get(job.id)
    assert done.stage == "done"
    assert statuses(done) == ["unknown", "success"]
    assert len(sender.calls()) == 2
    assert board.service.started == []
    board.scan()
    assert len(sender.calls()) == 2
    assert done.attempt == job.attempt


def test_resume_after_failures_gives_a_fresh_failure_budget(board: Board, sender: Sender):
    sender.modes("failure")
    job = finished_job(board)
    for _ in range(3):
        board.scan()
    assert board.get(job.id).wait_reason == "delivery_failed"
    cli(board, "resume", job.id)
    assert board.get(job.id).delivery_failures == 0
    board.scan()
    again = board.get(job.id)
    assert (again.stage, again.delivery_failures) == ("deliver_pending", 1)
    assert len(again.deliveries) == 4
    assert len(sender.calls()) == 4


def test_cancel_before_delivery_sends_nothing(board: Board, sender: Sender):
    job = finished_job(board)
    cli(board, "cancel", job.id)
    board.scan()
    board.scan()
    assert board.get(job.id).stage == "cancelled"
    assert sender.calls() == []
    assert board.get(job.id).deliveries == ()


def test_a_done_job_is_never_sent_again(board: Board, sender: Sender):
    job = finished_job(board)
    board.scan()
    assert board.get(job.id).stage == "done"
    before = board.get(job.id)
    board.scan()
    board.scan()
    assert len(sender.calls()) == 1
    assert board.get(job.id) == before


def test_a_job_already_done_without_attempts_is_not_sent(board: Board, sender: Sender):
    job_id = board.add()
    board.set(job_id, stage="done")
    board.scan()
    assert sender.calls() == []


def test_concurrent_scans_send_exactly_once(board: Board, sender: Sender):
    sender.modes("slow_success")
    job = finished_job(board)
    root = board.root / "home"

    def run(clock: int) -> None:
        scan_jobs(
            Jobs(root),
            board.config,
            now=board.clock + clock,
            boot_id="boot-1",
            service=board.service,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(run, range(4)))
    assert len(sender.calls()) == 1
    done = board.get(job.id)
    assert done.stage == "done"
    assert statuses(done) == ["success"]


def write_config(root: Path, **changes: object) -> None:
    data = {
        "version": 1,
        "slots": 2,
        "stall_seconds": 60,
        "omp_command": ["omp"],
        "rpc_timeout": 60,
        "deliver_command": ["send", "--to", "person.invalid", "--file", "{message}"],
        **changes,
    }
    data = {key: value for key, value in data.items() if value is not None}
    path = root / "taskwatch" / "jobs-config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_config_requires_the_delivery_command(tmp_path: Path):
    write_config(tmp_path, deliver_command=None)
    with pytest.raises(ValueError):
        load_job_config(tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        [],
        "send {message}",
        ["send", "--file", "x"],
        ["send", "{message}", "{message}"],
        ["send", "--file={message}"],
        ["send", "", "{message}"],
        ["send", " ", "{message}"],
        ["send", 1, "{message}"],
        ["send", "{message}{message}"],
    ],
)
def test_config_refuses_an_unsafe_delivery_command(tmp_path: Path, command: object):
    write_config(tmp_path, deliver_command=command)
    with pytest.raises(ValueError):
        load_job_config(tmp_path)


def test_literal_json_in_a_configured_sender_is_not_a_placeholder(board: Board):
    command = [
        sys.executable,
        "-c",
        'print(\'{"success": true, "message_id": "inline-message"}\')',
        "{message}",
    ]
    write_config(board.jobs.state.root, deliver_command=command)
    board.config = load_job_config(board.jobs.state.root)
    job = finished_job(board)
    board.scan()
    done = board.get(job.id)
    assert done.stage == "done"
    assert done.deliveries[-1]["message_id"] == "inline-message"
