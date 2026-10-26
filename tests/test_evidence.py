"""The card's evidence comes from the run record and the result as it is now."""

from datetime import datetime, timezone

import pytest

from taskwatch.evidence import (
    REJECTS,
    RUNS,
    EvidenceBoard,
    Run,
    append_run,
    basis_of,
    result_fingerprint,
)
from taskwatch.fingerprint import FileResult, GitResult, files_fingerprint
from taskwatch.protocol import BoardReadError, Card
from taskwatch.state import State
from tests.fake_board import FakeBoard
from tests.git_fixture import git, make_repo

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


def _layout(tmp_path):
    root = tmp_path / "hermes"
    root.mkdir()
    intent = tmp_path / "intent.md"
    spec = tmp_path / "spec.md"
    intent.write_text("why\n", encoding="utf-8")
    spec.write_text("what\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    (work / "out.txt").write_text("result\n", encoding="utf-8")
    card = Card(id="c1", status="review", gate=True, intent_path=str(intent), spec_path=str(spec))
    return State(root), card, work


def _run(card, work, **kwargs) -> Run:
    fields = {
        "run_id": "r1",
        "card": card.id,
        "kind": "work",
        "exit_code": 0,
        "copy": str(work),
        "git": False,
        "results": ("out.txt",),
    }
    fields.update(kwargs)
    return Run(**fields)


def test_card_is_filled_from_the_run_record_and_the_live_result(tmp_path):
    state, card, work = _layout(tmp_path)
    checklist = tmp_path / "check.json"
    append_run(state, _run(card, work, checklist=str(checklist)), NOW)
    seen = EvidenceBoard(FakeBoard((card,)), state).card("c1")
    assert seen.exit_code == 0
    assert seen.required_outputs == (str(work / "out.txt"),)
    assert seen.checklist_path == str(checklist)
    assert seen.fingerprint == files_fingerprint(work, ("out.txt",))
    assert seen.basis == basis_of(tmp_path / "intent.md", tmp_path / "spec.md", ())
    assert seen.dirty is False


def test_fields_a_card_carries_do_not_count(tmp_path):
    state, card, work = _layout(tmp_path)
    lying = Card(id="c1", status="review", exit_code=0, fingerprint="f" * 64, basis="b" * 64)
    seen = EvidenceBoard(FakeBoard((lying,)), state).card("c1")
    assert seen.exit_code is None
    assert seen.fingerprint is None
    assert seen.basis is None
    append_run(state, _run(card, work), NOW)
    seen = EvidenceBoard(FakeBoard((lying,)), state).card("c1")
    assert seen.fingerprint == files_fingerprint(work, ("out.txt",))
    assert seen.basis is None


def test_changed_result_and_changed_intent_change_the_fingerprint_and_basis(tmp_path):
    state, card, work = _layout(tmp_path)
    append_run(state, _run(card, work), NOW)
    board = EvidenceBoard(FakeBoard((card,)), state)
    before = board.card("c1")
    (work / "out.txt").write_text("changed\n", encoding="utf-8")
    (tmp_path / "intent.md").write_text("another purpose\n", encoding="utf-8")
    after = board.card("c1")
    assert after.fingerprint != before.fingerprint
    assert after.basis != before.basis


def test_missing_result_has_no_fingerprint(tmp_path):
    state, card, work = _layout(tmp_path)
    append_run(state, _run(card, work, results=("gone.txt",)), NOW)
    assert EvidenceBoard(FakeBoard((card,)), state).card("c1").fingerprint is None


def test_git_result_fingerprint_is_head_and_dirty_is_live(tmp_path):
    state, card, _ = _layout(tmp_path)
    repo = make_repo(tmp_path)
    append_run(state, _run(card, repo, git=True), NOW)
    board = EvidenceBoard(FakeBoard((card,)), state)
    first = board.card("c1")
    assert first.fingerprint == git(repo, "rev-parse", "HEAD")
    assert first.dirty is False
    (repo / "out.txt").write_text("edited\n", encoding="utf-8")
    assert board.card("c1").dirty is True
    git(repo, "commit", "-am", "more")
    later = board.card("c1")
    assert later.fingerprint == git(repo, "rev-parse", "HEAD")
    assert later.fingerprint != first.fingerprint
    assert later.dirty is False


def test_unreadable_git_copy_is_dirty_and_has_no_fingerprint(tmp_path):
    state, card, _ = _layout(tmp_path)
    append_run(state, _run(card, tmp_path / "missing", git=True), NOW)
    seen = EvidenceBoard(FakeBoard((card,)), state).card("c1")
    assert seen.fingerprint is None
    assert seen.dirty is True


def test_work_run_wins_over_a_review_run_and_guidelines_come_from_the_review(tmp_path):
    state, card, work = _layout(tmp_path)
    rule = tmp_path / "rules.md"
    rule.write_text("rules\n", encoding="utf-8")
    append_run(state, _run(card, work, run_id="w1"), NOW)
    append_run(
        state,
        _run(card, work, run_id="v1", kind="review", exit_code=3, guidelines=(str(rule),)),
        NOW,
    )
    seen = EvidenceBoard(FakeBoard((card,)), state).card("c1")
    assert seen.exit_code == 0
    assert seen.basis == basis_of(tmp_path / "intent.md", tmp_path / "spec.md", (rule,))


def test_a_review_card_uses_its_own_review_run(tmp_path):
    state, card, work = _layout(tmp_path)
    append_run(state, _run(card, work, kind="review"), NOW)
    assert EvidenceBoard(FakeBoard((card,)), state).card("c1").exit_code == 0


def test_reject_count_is_the_finished_rejects_in_the_log_once_per_id(tmp_path):
    state, card, _ = _layout(tmp_path)
    for card_id, reject_id, phase in (
        ("c1", "r1", "started"),
        ("c1", "r1", "done"),
        ("c1", "r1", "done"),
        ("c1", "r2", "started"),
        ("c2", "r9", "started"),
    ):
        state.append_record(
            REJECTS, {"card_id": card_id, "reject_id": reject_id, "phase": phase}, NOW
        )
    assert EvidenceBoard(FakeBoard((card,)), state).card("c1").reject_count == 1


def test_a_bad_record_is_a_board_error_not_an_empty_result(tmp_path):
    state, card, _ = _layout(tmp_path)
    state.append_record(RUNS, {"card": "c1"}, NOW)
    board = EvidenceBoard(FakeBoard((card,)), state)
    assert "runs.jsonl:1" in board.read_error()
    with pytest.raises(BoardReadError):
        board.card("c1")
    with pytest.raises(BoardReadError):
        board.cards()


def test_result_names_exactly_the_checked_fingerprint(tmp_path):
    state, card, work = _layout(tmp_path)
    append_run(state, _run(card, work), NOW)
    board = EvidenceBoard(FakeBoard((card,)), state)
    assert board.result(board.card("c1")) == FileResult(work, ("out.txt",))
    repo = make_repo(tmp_path)
    append_run(state, _run(card, repo, git=True, run_id="r2"), NOW)
    checked = board.card("c1")
    assert board.result(checked) == GitResult(repo, checked.fingerprint)


def test_result_fingerprint_matches_the_library_hash(tmp_path):
    _, _, work = _layout(tmp_path)
    assert result_fingerprint(work, False, ("out.txt",)) == (
        files_fingerprint(work, ("out.txt",)),
        False,
    )
