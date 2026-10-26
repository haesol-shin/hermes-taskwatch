"""The worker example writes the run the gate reads and hands the approved snapshot on."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from taskwatch.config import config_path
from taskwatch.evidence import EvidenceBoard
from taskwatch.fingerprint import files_fingerprint
from taskwatch.protocol import Card
from taskwatch.state import State
from tests.config_fixture import config_dict, write_config
from tests.fake_board import FakeBoard

ROOT = Path(__file__).resolve().parents[1]
STUB = Path(__file__).resolve().parent / "executor_stub.py"


def _script():
    spec = importlib.util.spec_from_file_location(
        "run_executor_example", ROOT / "examples/worker/run_executor.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _World:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.root = tmp_path / "hermes"
        raw = config_dict()
        raw["hermes_root"] = str(self.root)
        template = [sys.executable, str(STUB), "--brief", "{brief}", "--copy", "{copy}"]
        template += ["--output", "{output}"]
        raw["executors"] = {"work": template, "review": template}
        self.config = write_config(config_path(self.root), raw)
        self.intent = tmp_path / "intent.md"
        self.spec = tmp_path / "spec.md"
        self.intent.write_text("why\n", encoding="utf-8")
        self.spec.write_text("what\n", encoding="utf-8")
        self.work = tmp_path / "work"
        self.work.mkdir()

    def card(self, card_id="c1", **fields) -> Card:
        return Card(
            id=card_id,
            status="running",
            intent_path=str(self.intent),
            spec_path=str(self.spec),
            **fields,
        )

    def brief(self, **plan) -> str:
        path = self.tmp / f"brief-{len(list(self.tmp.glob('brief-*')))}.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        return str(path)

    def run(self, kind, cards, *extra, brief=None, output=None):
        args = [
            "--config",
            str(self.config),
            "--kind",
            kind,
            "--card",
            "c1",
            "--brief",
            brief or self.brief(),
            "--copy",
            str(self.work),
            "--output",
            output or str(self.tmp / "out.md"),
            *extra,
        ]
        return _script().main(args, board=FakeBoard(tuple(cards)))


def _line(capfd) -> dict:
    out, _ = capfd.readouterr()
    lines = out.splitlines()
    assert len(lines) == 1, out
    return json.loads(lines[0])


def test_stdout_is_one_json_line_even_when_the_executor_prints(tmp_path, capfd):
    world = _World(tmp_path)
    brief = world.brief(print="noise from the executor", write={str(world.work / "out.txt"): "x"})
    code = world.run("work", [world.card()], "--result", "out.txt", brief=brief)
    result = _line(capfd)
    assert code == 0
    assert result["ok"] is True
    assert "noise from the executor" in Path(result["log"]).read_text(encoding="utf-8")


def test_the_run_record_is_what_the_gate_reads(tmp_path, capfd):
    world = _World(tmp_path)
    checklist = tmp_path / "check.json"
    checklist.write_text('{"items":[{"id":"a","passes":true}]}', encoding="utf-8")
    brief = world.brief(write={str(world.work / "out.txt"): "result\n"})
    world.run(
        "work", [world.card()], "--result", "out.txt", "--checklist", str(checklist), brief=brief
    )
    assert _line(capfd)["ok"] is True
    seen = EvidenceBoard(FakeBoard((world.card(),)), State(world.root)).card("c1")
    assert seen.exit_code == 0
    assert seen.checklist_path == str(checklist)
    assert seen.fingerprint == files_fingerprint(world.work, ("out.txt",))


def test_a_missing_required_output_is_a_block_and_exit_one(tmp_path, capfd):
    world = _World(tmp_path)
    code = world.run("work", [world.card()], "--result", "out.txt")
    result = _line(capfd)
    assert code == 1
    assert result["ok"] is False
    assert "out.txt" in result["block"]


def _snapshot(tmp_path) -> Path:
    snapshot = tmp_path / "snapshots" / "approved"
    snapshot.mkdir(parents=True)
    (snapshot / "in.txt").write_text("approved input\n", encoding="utf-8")
    return snapshot


def test_the_next_card_gets_its_own_work_copy_of_the_approved_snapshot(tmp_path, capfd):
    world = _World(tmp_path)
    snapshot = _snapshot(tmp_path)
    (world.work / "old.txt").write_text("earlier work\n", encoding="utf-8")
    parent = world.card("p1", gate=True, snapshot_path=str(snapshot))
    child = world.card(parents=("p1",))
    seen = tmp_path / "seen-copy"
    brief = world.brief(echo_copy=str(seen), write_in_copy={"out.txt": "next\n"})
    world.run("work", [parent, child], "--result", "out.txt", brief=brief)
    assert _line(capfd)["ok"] is True
    # The executor wrote into the copy it was given, and that copy is the one checked and recorded.
    assert seen.read_text(encoding="utf-8") == str(world.work)
    assert (world.work / "in.txt").read_text(encoding="utf-8") == "approved input\n"
    assert (world.work / "out.txt").read_text(encoding="utf-8") == "next\n"
    recorded = EvidenceBoard(FakeBoard((child,)), State(world.root)).card("c1")
    assert recorded.fingerprint == files_fingerprint(world.work, ("out.txt",))
    assert recorded.output_paths == (str(world.work / "out.txt"),)
    # The snapshot did not change, and the earlier work copy was kept under another name.
    assert sorted(path.name for path in snapshot.iterdir()) == ["in.txt"]
    assert (snapshot / "in.txt").read_text(encoding="utf-8") == "approved input\n"
    assert not (world.work / "old.txt").exists()
    (kept,) = tmp_path.glob("work.before-*")
    assert (kept / "old.txt").read_text(encoding="utf-8") == "earlier work\n"


def test_a_card_without_an_approved_parent_gets_its_own_copy(tmp_path, capfd):
    world = _World(tmp_path)
    seen = tmp_path / "seen-copy"
    brief = world.brief(echo_copy=str(seen), write={str(world.work / "out.txt"): "x"})
    world.run("work", [world.card()], "--result", "out.txt", brief=brief)
    assert _line(capfd)["ok"] is True
    assert seen.read_text(encoding="utf-8") == str(world.work)


def test_a_parent_that_is_not_a_gate_does_not_change_the_copy(tmp_path, capfd):
    world = _World(tmp_path)
    seen = tmp_path / "seen-copy"
    brief = world.brief(echo_copy=str(seen), write={str(world.work / "out.txt"): "x"})
    world.run(
        "work", [world.card("p1"), world.card(parents=("p1",))], "--result", "out.txt", brief=brief
    )
    assert _line(capfd)["ok"] is True
    assert seen.read_text(encoding="utf-8") == str(world.work)


@pytest.mark.parametrize("problem", ["no snapshot", "snapshot gone", "parent unreadable"])
def test_a_card_behind_a_gate_without_a_usable_snapshot_is_blocked_before_it_runs(
    tmp_path, capfd, problem
):
    world = _World(tmp_path)
    ran = tmp_path / "ran"
    brief = world.brief(echo_copy=str(ran), write_in_copy={"out.txt": "x"})
    parents = {
        "no snapshot": [world.card("p1", gate=True)],
        "snapshot gone": [world.card("p1", gate=True, snapshot_path=str(tmp_path / "gone"))],
        "parent unreadable": [],
    }[problem]
    world.run("work", [*parents, world.card(parents=("p1",))], "--result", "out.txt", brief=brief)
    result = _line(capfd)
    assert result["ok"] is False
    assert "parent" in result["block"] or "snapshot" in result["block"]
    assert not ran.exists()
    assert not (world.work / "out.txt").exists()


def test_a_review_through_the_script_reads_the_output_the_run_wrote(tmp_path, capfd):
    world = _World(tmp_path)
    (world.work / "out.txt").write_text("result\n", encoding="utf-8")
    line = '{"verdict": "PASS", "blocking": false, "reviewer": "reviewer.invalid"}'
    brief = world.brief(output=f"All good.\n{line}\n")
    world.run("review", [world.card()], "--result", "out.txt", brief=brief)
    result = _line(capfd)
    assert result["ok"] is True
    assert result["verdict"] == "PASS"
    review = State(world.root).load().reviews[0]
    assert review.fingerprint == files_fingerprint(world.work, ("out.txt",))


def test_an_executor_that_cannot_start_is_a_block_with_a_log(tmp_path, capfd):
    world = _World(tmp_path)
    raw = json.loads(world.config.read_text(encoding="utf-8"))
    raw["executors"]["work"] = [str(tmp_path / "no-such-executor"), "{brief}", "{copy}", "{output}"]
    world.config.write_text(json.dumps(raw), encoding="utf-8")
    world.run("work", [world.card()], "--result", "out.txt")
    result = _line(capfd)
    assert result["ok"] is False
    assert result["exit_code"] == 127


def test_unknown_card_is_a_block(tmp_path, capfd):
    world = _World(tmp_path)
    assert world.run("work", [], "--result", "out.txt") == 1
    assert "unknown card" in _line(capfd)["block"]


@pytest.mark.parametrize("missing", ["intent_path", "spec_path"])
def test_a_card_without_intent_or_spec_is_a_block(tmp_path, capfd, missing):
    world = _World(tmp_path)
    card = Card(id="c1", status="running", **{missing: None})
    world.run("work", [card], "--result", "out.txt")
    assert _line(capfd)["ok"] is False
