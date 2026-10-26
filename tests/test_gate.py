"""The gate blocks a failed check and ignores a call that is not its target."""

from datetime import datetime, timezone
from pathlib import Path

from taskwatch.config import load_config
from taskwatch.gate import decide, pre_tool_call
from taskwatch.protocol import Card
from taskwatch.state import State
from tests.config_fixture import write_config
from tests.fake_board import FakeBoard
from tests.test_checks import _pass, _ready

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)
DB = "/placeholder/board.invalid.db"
OTHER = "/placeholder/other.invalid.db"


def _config(tmp_path):
    root = tmp_path / "hermes"
    root.mkdir(exist_ok=True)
    return load_config(write_config(root / "taskwatch" / "config.json"))


def _call(tmp_path, tool, args, **kwargs):
    config = kwargs.pop("config", _config(tmp_path))
    board = kwargs.pop("board", FakeBoard((Card(id="c1", status="running", exit_code=1),)))
    recorded = {}

    def record(card_id, path):
        recorded[card_id] = path

    result = decide(
        tool_name=tool,
        args=args,
        profile_name=kwargs.get("profile", "main"),
        env_task=kwargs.get("env_task"),
        db_path=kwargs.get("db_path", DB),
        config=config,
        config_error=kwargs.get("config_error"),
        board=board,
        state=State(tmp_path / "hermes"),
        now=NOW,
        freezer=kwargs.get("freezer"),
        record_snapshot=kwargs.get("record", record),
        configured_db=kwargs.get("configured_db", DB),
    )
    return result, board, recorded


def test_failing_complete_blocks_and_leaves_the_card(tmp_path):
    result, board, recorded = _call(tmp_path, "kanban_complete", {"task_id": "c1"})
    assert result["action"] == "block"
    assert "exit" in result["message"]
    assert board.card("c1").status == "running"
    assert recorded == {}


def test_omitted_task_id_uses_the_env_and_ignores_the_hook_id(tmp_path):
    blocked, _, _ = _call(tmp_path, "kanban_complete", {}, env_task="c1")
    assert blocked["action"] == "block"
    allowed, _, _ = _call(tmp_path, "kanban_complete", {}, env_task=None)
    assert allowed["message"] == "unresolved target"
    ignored = pre_tool_call(
        "kanban_complete",
        {},
        task_id="not-the-card",
        profile_name="main",
        env_task="c1",
        db_path=DB,
        configured_db=DB,
        config=_config(tmp_path),
        board=FakeBoard((Card(id="c1", status="running", exit_code=1),)),
        state=State(tmp_path / "hermes"),
        now=NOW,
    )
    assert ignored["action"] == "block"
    assert "not-the-card" not in ignored["message"]


def test_outside_profile_or_other_database_is_not_blocked(tmp_path):
    outside, board, _ = _call(tmp_path, "kanban_complete", {"task_id": "c1"}, profile="other")
    assert outside is None
    assert board.card("c1").status == "running"
    other_db, _, _ = _call(tmp_path, "kanban_complete", {"task_id": "c1"}, db_path=OTHER)
    assert other_db is None


def test_unreadable_config_inside_a_listed_profile_blocks(tmp_path):
    result, _, _ = _call(
        tmp_path,
        "kanban_complete",
        {"task_id": "c1"},
        config=None,
        config_error="unreadable config",
    )
    assert result["action"] == "block"
    assert "unreadable" in result["message"]


def test_passing_gate_approval_freezes_and_records_the_snapshot(tmp_path):
    card = _ready(tmp_path, gate=True, status="review", fingerprint="a" * 40, basis="basis-1")
    board = FakeBoard((card,))
    config = _config(tmp_path)
    state = State(tmp_path / "hermes")
    state.append_review(_pass(fingerprint="a" * 40, basis="basis-1"), "pass\n", NOW)
    frozen = {}

    def freezer(item, directory):
        frozen["card"] = item.id
        return directory / "snapshots" / item.fingerprint

    result = decide(
        tool_name="kanban_complete",
        args={"task_id": card.id},
        profile_name="main",
        env_task=None,
        db_path=DB,
        config=config,
        config_error=None,
        board=board,
        state=state,
        now=NOW,
        freezer=freezer,
        record_snapshot=lambda card_id, path: frozen.setdefault("path", path),
        configured_db=DB,
    )
    assert result is None
    assert frozen["card"] == card.id
    assert card.fingerprint in frozen["path"]
    assert board.card(card.id).status == "review"


def test_uninjected_gate_uses_the_live_context(tmp_path, monkeypatch):
    seen = {}
    board = FakeBoard((Card(id="c1", status="running", exit_code=1),))

    def fake(args, kwargs):
        del kwargs
        seen["task"] = args["task_id"]
        return {
            "profile_name": "main",
            "env_task": None,
            "db_path": DB,
            "config": _config(tmp_path),
            "config_error": None,
            "board": board,
            "state": State(tmp_path / "hermes"),
            "configured_db": DB,
        }

    monkeypatch.setattr("taskwatch.gate.gate_context", fake)
    result = pre_tool_call("kanban_complete", {"task_id": "c1"})
    assert seen["task"] == "c1"
    assert result["action"] == "block"
    assert pre_tool_call("terminal", {}) is None


def test_snapshot_path_replaces_the_card_block():
    from taskwatch.card_body import loads, replace_fields

    body = replace_fields(
        'note\n<!-- taskwatch\n{"gate": true}\n-->\n',
        {"snapshot_path": "/placeholder/snap"},
    )
    assert loads(body)["snapshot_path"] == "/placeholder/snap"
    assert "note" in body
    assert body.count("<!-- taskwatch") == 1


class _Reviewed:
    """A card whose work ran and whose result got a PASS, with real files behind it."""

    def __init__(self, tmp_path, git=False):
        from taskwatch.evidence import EvidenceBoard, Run, append_run, basis_of, result_fingerprint
        from taskwatch.state import RECORD_VERSION, Review

        self.root = tmp_path / "hermes"
        self.config = _config(tmp_path)
        self.state = State(self.root)
        self.intent = tmp_path / "intent.md"
        self.spec = tmp_path / "spec.md"
        self.intent.write_text("why\n", encoding="utf-8")
        self.spec.write_text("what\n", encoding="utf-8")
        if git:
            from tests.git_fixture import make_repo

            self.work = make_repo(tmp_path)
        else:
            self.work = tmp_path / "work"
            self.work.mkdir()
            (self.work / "out.txt").write_text("result\n", encoding="utf-8")
        checklist = tmp_path / "check.json"
        checklist.write_text('{"items":[{"id":"a","passes":true}]}', encoding="utf-8")
        run = Run("r1", "c1", "work", 0, str(self.work), git, ("out.txt",), str(checklist))
        append_run(self.state, run, NOW)
        fingerprint, _ = result_fingerprint(self.work, git, ("out.txt",))
        basis = basis_of(self.intent, self.spec, ())
        review = Review(
            RECORD_VERSION, 1, "c1", fingerprint, basis, "reviewer.invalid", "PASS", False
        )
        self.state.append_review(review, "pass\n", NOW)
        card = Card(
            id="c1",
            status="review",
            gate=True,
            intent_path=str(self.intent),
            spec_path=str(self.spec),
        )
        self.board = EvidenceBoard(FakeBoard((card,)), self.state)
        self.recorded = {}

    def approve(self, freezer=None):
        from taskwatch.fingerprint import freeze

        def default(card, directory):
            return freeze(self.board.result(card), directory)

        return decide(
            tool_name="kanban_complete",
            args={"task_id": "c1"},
            profile_name="main",
            env_task=None,
            db_path=DB,
            config=self.config,
            config_error=None,
            board=self.board,
            state=self.state,
            now=NOW,
            freezer=freezer or default,
            record_snapshot=lambda card_id, path: self.recorded.setdefault(card_id, path),
            configured_db=DB,
        )


def test_gate_approves_the_reviewed_result_and_freezes_exactly_it(tmp_path):
    from taskwatch.fingerprint import files_fingerprint

    reviewed = _Reviewed(tmp_path)
    assert reviewed.approve() is None
    snapshot = Path(reviewed.recorded["c1"])
    assert snapshot.name == files_fingerprint(reviewed.work, ("out.txt",))
    assert (snapshot / "out.txt").read_text(encoding="utf-8") == "result\n"


def test_gate_refuses_a_result_changed_after_the_pass(tmp_path):
    reviewed = _Reviewed(tmp_path)
    (reviewed.work / "out.txt").write_text("UNREVIEWED\n", encoding="utf-8")
    blocked = reviewed.approve()
    assert blocked["action"] == "block"
    assert "no PASS" in blocked["message"]
    assert reviewed.recorded == {}


def test_gate_refuses_when_the_intent_changed_after_the_pass(tmp_path):
    reviewed = _Reviewed(tmp_path)
    reviewed.intent.write_text("a different purpose\n", encoding="utf-8")
    assert reviewed.approve()["action"] == "block"
    assert reviewed.recorded == {}


def test_gate_refuses_a_git_copy_with_uncommitted_changes_or_new_commits(tmp_path):
    from tests.git_fixture import git

    reviewed = _Reviewed(tmp_path, git=True)
    (reviewed.work / "out.txt").write_text("edited\n", encoding="utf-8")
    dirty = reviewed.approve()
    assert dirty["action"] == "block"
    assert "changed after the PASS" in dirty["message"]
    git(reviewed.work, "commit", "-am", "more")
    moved = reviewed.approve()
    assert moved["action"] == "block"
    assert "no PASS" in moved["message"]
    assert reviewed.recorded == {}


def test_gate_refuses_when_the_result_changes_while_it_is_frozen(tmp_path):
    from taskwatch.fingerprint import freeze

    reviewed = _Reviewed(tmp_path)

    def late_change(card, directory):
        (reviewed.work / "out.txt").write_text("changed in between\n", encoding="utf-8")
        return freeze(reviewed.board.result(card), directory)

    blocked = reviewed.approve(late_change)
    assert blocked["action"] == "block"
    assert "changed while it was frozen" in blocked["message"]
    assert reviewed.recorded == {}


def _live_call(tmp_path, monkeypatch, pinned, **args):
    """pre_tool_call through gate_context, with Hermes' path function replaced by a model."""
    config = _config(tmp_path)

    def model(slug, *, honor_env):
        # Hermes: a pin wins over any name; without it, a name picks that board's file.
        if honor_env and pinned:
            return OTHER
        return DB if slug in (None, config.board) else OTHER

    monkeypatch.setattr("taskwatch.hermes_board.db_path", model)

    class Raw(FakeBoard):
        def record_snapshot(self, card_id, path):
            raise AssertionError("nothing may be frozen here")

    monkeypatch.setattr(
        "taskwatch.hermes_board.open_board",
        lambda slug=None, db_path=None: Raw((Card(id="c1", status="running"),)),
    )
    return pre_tool_call(
        "kanban_complete",
        {"task_id": "c1", **args},
        profile_name="main",
        root=tmp_path / "hermes",
        env_task=None,
    )


def test_target_is_the_database_the_call_really_opens(tmp_path, monkeypatch):
    # The pin sends the call to another database, even though the arguments name the target.
    assert _live_call(tmp_path, monkeypatch, True, board=_config(tmp_path).board) is None
    # Without the pin the named board is the target and the gate runs.
    unpinned = _live_call(tmp_path, monkeypatch, False, board=_config(tmp_path).board)
    assert unpinned["action"] == "block"
