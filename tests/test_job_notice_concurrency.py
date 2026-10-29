"""A blocked notice sender must not block runners or overwrite newer job state."""

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from taskwatch.job_cli import main as cli_main
from taskwatch.job_runner import run_job
from tests.test_job_runner import GOOD_FILES, PASS, Env
from tests.test_job_scan import Board

GATED_SENDER = """
import json, pathlib, sys, time
message = pathlib.Path(sys.argv[1])
gate = pathlib.Path(sys.argv[2])
if message.name.startswith('notice-'):
    gate.with_suffix('.started').touch()
    while not gate.exists():
        time.sleep(0.01)
print(json.dumps({'success': True, 'message_id': message.name}))
"""


def test_blocked_notice_allows_real_step_and_delivery(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.seed(GOOD_FILES)
    env.scenario([{"reply": PASS}])
    env.put("check")
    board = Board(tmp_path)
    board.count = 1
    board.jobs = env.jobs
    gate = tmp_path / "release"
    board.config = replace(
        env.config,
        deliver_command=(sys.executable, "-c", GATED_SENDER, "{message}", str(gate)),
    )
    board.service.states["u"] = "active"
    waiting = board.add(kind="document")
    board.set(waiting, stage="user_wait", wait_reason="synthetic_stop")

    def finish_and_deliver():
        board.scan()
        assert board.get(waiting).notices[0]["status"] == "started"
        assert (
            cli_main(
                ["--root", str(env.jobs.state.root), "cancel", waiting, "--reason", "operator"]
            )
            == 0
        )
        assert run_job(env.jobs, env.config, env.job.id, env.attempt) == 0
        assert env.get().stage == "review"
        assert env.run("review") == 0
        board.service.states["u"] = "inactive"
        board.scan()
        assert env.get().stage == "done"
        assert env.get().deliveries[-1]["message_id"] == "delivery.txt"

    with ThreadPoolExecutor(max_workers=2) as pool:
        notice = pool.submit(board.scan)
        try:
            deadline = time.monotonic() + 5
            while not gate.with_suffix(".started").exists():
                assert time.monotonic() < deadline, "notice sender did not reach the gate"
                time.sleep(0.01)
            pool.submit(finish_and_deliver).result(timeout=5)
            assert board.get(waiting).notices[0]["status"] == "started"
        finally:
            gate.touch()
        notice.result(timeout=5)
    stopped = board.get(waiting)
    assert stopped.stage == "cancelled"
    assert [record["status"] for record in stopped.notices] == ["success"]
    board.scan()
    assert board.get(waiting) == stopped
