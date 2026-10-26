import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from taskwatch.checks import (
    REGISTRY,
    Cause,
    CheckInput,
    WatchTarget,
    format_cause,
    gate,
    internal_rewarn_due,
    received_at,
    user_notify_due,
    watch,
)
from taskwatch.config import load_config
from taskwatch.protocol import BoardReader, BoardReadError, Card, Comment, Event, Run
from taskwatch.state import RECORD_VERSION, Approval, Review, Warning
from tests.config_fixture import write_config
from tests.fake_board import FakeBoard

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


def _config(tmp_path):
    return load_config(write_config(tmp_path / "config.json"))


def _target(config, profile="main"):
    return WatchTarget(config.hermes_root, config.board, profile)


def _input(config, board, **kwargs):
    now = kwargs.pop("now", NOW)
    kwargs.setdefault("target", _target(config))
    return CheckInput(now=now, config=config, board=board, **kwargs)


def _card(card_id="card-1", **kwargs) -> Card:
    return Card(id=card_id, status=kwargs.pop("status", "ready"), **kwargs)


def _event(event_id, kind, at) -> Event:
    return Event(event_id, kind, at)


def _pass(card="card-1", fingerprint="fp-1", basis="basis-1") -> Review:
    return Review(RECORD_VERSION, 1, card, fingerprint, basis, "reviewer.invalid", "PASS", False)


def _start(key="apr-1", at=NOW) -> Approval:
    return Approval(RECORD_VERSION, "start", key, approved_at=at, approved="start the work")


def _apply(fingerprint="fp-1") -> Approval:
    return Approval(
        RECORD_VERSION,
        "apply",
        None,
        approved="merge",
        message_id="msg-1",
        card_id="card-1",
        fingerprint=fingerprint,
    )


def _checklist(path, passes):
    path.write_text(
        json.dumps({"items": [{"id": "item-1", "passes": passes}]}),
        encoding="utf-8",
    )
    return path


def _ready(tmp_path, **kwargs) -> Card:
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = tmp_path / "out.txt"
    out.write_text("ok\n", encoding="utf-8")
    checklist = _checklist(tmp_path / "checklist.json", True)
    fields = {
        "status": "running",
        "exit_code": 0,
        "required_outputs": (str(out),),
        "checklist_path": str(checklist),
        "fingerprint": "fp-1",
        "basis": "basis-1",
    }
    fields.update(kwargs)
    return _card(**fields)


def _ids(decision) -> set[str]:
    return {cause.check for cause in decision.causes}


def test_board_reader_has_no_write_methods():
    names = {name for name in vars(BoardReader) if not name.startswith("_")}
    assert names == {"card", "cards", "open_wait_ids", "read_error"}


def test_registry_records_what_each_check_blocks():
    assert REGISTRY
    blocks = [spec.blocks for spec in REGISTRY.values()]
    removable = [spec.removable_when for spec in REGISTRY.values()]
    assert len(blocks) == len(set(blocks))
    assert len(removable) == len(set(removable))
    assert all(item and "\n" not in item for item in blocks + removable)


def test_cause_body_has_one_next_line_and_a_certainty_mark():
    cause = Cause("gate_action", "card-1:ev-1", "no action", "Record done", True)
    lines = format_cause(cause).splitlines()
    assert [line for line in lines if line.startswith("next:")] == ["next: Record done"]
    assert lines[-1] == "certainty: certain"
    uncertain = Cause("watch", "watch:unreadable", "bad", "Fix the record", False, True)
    assert format_cause(uncertain).endswith("certainty: uncertain")


def test_checklist_failure_blocks_complete_and_a_pass_allows(tmp_path):
    config = _config(tmp_path)
    failing = _ready(tmp_path)
    failing = Card(
        **{**failing.__dict__, "checklist_path": str(_checklist(tmp_path / "bad.json", False))}
    )
    blocked = gate(
        "complete",
        _input(config, FakeBoard((failing,)), card_id="card-1"),
        profile_in_list=True,
        board_matches=True,
    )
    assert blocked.block
    assert "worker_finish" in _ids(blocked)

    allowed = gate(
        "complete",
        _input(config, FakeBoard((_ready(tmp_path / "ok"),)), card_id="card-1"),
        profile_in_list=True,
        board_matches=True,
    )
    assert allowed.checked and not allowed.block and allowed.causes == ()


def test_nonzero_exit_or_missing_output_blocks_complete(tmp_path):
    config = _config(tmp_path)
    bad_exit = Card(**{**_ready(tmp_path).__dict__, "exit_code": 2})
    blocked = gate(
        "complete",
        _input(config, FakeBoard((bad_exit,)), card_id="card-1"),
        profile_in_list=True,
        board_matches=True,
    )
    assert blocked.block and any(cause.cause_id.endswith(":exit") for cause in blocked.causes)
    missing = Card(
        **{**_ready(tmp_path).__dict__, "required_outputs": ("/placeholder/missing.txt",)}
    )
    blocked = gate(
        "complete",
        _input(config, FakeBoard((missing,)), card_id="card-1"),
        profile_in_list=True,
        board_matches=True,
    )
    assert blocked.block and any(cause.cause_id.endswith(":outputs") for cause in blocked.causes)


def test_old_fingerprint_pass_blocks_and_current_pass_allows(tmp_path):
    config = _config(tmp_path)
    card = _ready(tmp_path, status="review", gate=True)
    old = gate(
        "approve",
        _input(config, FakeBoard((card,)), card_id="card-1", reviews=(_pass(fingerprint="old"),)),
        profile_in_list=True,
        board_matches=True,
    )
    assert old.block and "pass_review" in _ids(old)
    current = gate(
        "approve",
        _input(config, FakeBoard((card,)), card_id="card-1", reviews=(_pass(),)),
        profile_in_list=True,
        board_matches=True,
    )
    assert not current.block


def test_old_basis_pass_blocks_and_current_basis_allows(tmp_path):
    config = _config(tmp_path)
    card = _ready(tmp_path, status="review", gate=True)
    blocked = gate(
        "approve",
        _input(config, FakeBoard((card,)), card_id="card-1", reviews=(_pass(basis="old-basis"),)),
        profile_in_list=True,
        board_matches=True,
    )
    assert blocked.block and "pass_review" in _ids(blocked)
    allowed = gate(
        "approve",
        _input(config, FakeBoard((card,)), card_id="card-1", reviews=(_pass(),)),
        profile_in_list=True,
        board_matches=True,
    )
    assert not allowed.block


def test_dirty_work_copy_blocks_approve_and_apply(tmp_path):
    config = _config(tmp_path)
    clean = _ready(tmp_path, status="review", gate=True)
    dirty = Card(**{**clean.__dict__, "dirty": True})
    approval = _apply()
    for action in ("approve", "apply"):
        blocked = gate(
            action,
            _input(
                config,
                FakeBoard((dirty,)),
                card_id="card-1",
                reviews=(_pass(),),
                approvals=(approval,),
            ),
            profile_in_list=True,
            board_matches=True,
        )
        assert blocked.block and "clean_result" in _ids(blocked)
    allowed = gate(
        "apply",
        _input(
            config,
            FakeBoard((clean,)),
            card_id="card-1",
            reviews=(_pass(),),
            approvals=(approval,),
        ),
        profile_in_list=True,
        board_matches=True,
    )
    assert not allowed.block


def test_apply_fingerprint_mismatch_blocks(tmp_path):
    config = _config(tmp_path)
    card = _ready(tmp_path, status="review", gate=True)
    old = _apply("old")
    blocked = gate(
        "apply",
        _input(
            config,
            FakeBoard((card,)),
            card_id="card-1",
            reviews=(_pass(),),
            approvals=(old,),
        ),
        profile_in_list=True,
        board_matches=True,
    )
    assert blocked.block and "apply_match" in _ids(blocked)


def test_read_failure_warns_and_blocks_instead_of_passing(tmp_path):
    config = _config(tmp_path)
    card = _ready(
        tmp_path,
        status="running",
        runs=(Run("run-1", NOW - timedelta(hours=3), "running"),),
    )
    board = FakeBoard((card,), error="board down")
    warned = watch(_input(config, board))
    assert not warned.block
    assert warned.causes[0].uncheckable
    assert "stalled" not in _ids(warned)
    blocked = gate(
        "complete",
        _input(config, board, card_id="card-1"),
        profile_in_list=True,
        board_matches=True,
        config_error="unreadable config",
    )
    assert blocked.block and blocked.causes[0].uncheckable
    record = gate(
        "complete",
        _input(config, FakeBoard((card,)), card_id="card-1"),
        profile_in_list=True,
        board_matches=True,
        record_error="approvals.jsonl:2: unknown version",
    )
    assert record.block and record.causes[0].uncheckable
    raised = watch(_input(config, FakeBoard(boom=True)))
    assert raised.causes[0].uncheckable
    assert raised.causes[0].summary != ""


def test_target_mismatch_skips_other_checks(tmp_path):
    config = _config(tmp_path)
    card = _ready(
        tmp_path,
        status="running",
        runs=(Run("run-1", NOW - timedelta(hours=3), "running"),),
    )
    other = WatchTarget("/placeholder/other", config.board, "main")
    decision = watch(_input(config, FakeBoard((card,)), target=other))
    assert not decision.checked
    assert _ids(decision) == {"target_mismatch"}


def test_gate_skips_outside_profile_and_blocks_unreadable_inside(tmp_path):
    config = _config(tmp_path)
    card = _ready(tmp_path)
    failing = Card(
        **{**card.__dict__, "checklist_path": str(_checklist(tmp_path / "bad.json", False))}
    )
    skipped = gate(
        "complete",
        _input(config, FakeBoard((failing,)), card_id="card-1"),
        profile_in_list=False,
        board_matches=True,
        config_error="unreadable config",
    )
    assert not skipped.checked and not skipped.block and skipped.causes == ()
    other_board = gate(
        "complete",
        _input(config, FakeBoard((failing,)), card_id="card-1"),
        profile_in_list=True,
        board_matches=False,
    )
    assert not other_board.block and other_board.causes == ()
    unresolved = gate(
        "complete",
        _input(config, FakeBoard((card,)), card_id="card-1"),
        profile_in_list=True,
        board_matches=None,
    )
    assert unresolved.block and unresolved.causes[0].uncheckable
    inside = gate(
        "complete",
        _input(config, FakeBoard((card,)), card_id="card-1"),
        profile_in_list=True,
        board_matches=True,
        config_error="unreadable config",
    )
    assert inside.block and inside.causes[0].uncheckable


def test_gate_action_ignores_comments_and_ledger_events(tmp_path):
    config = _config(tmp_path)
    event = _event("ev-1", "review_requested", NOW - HOUR)
    comment = Comment("handled event=ev-1 action=approve", NOW - timedelta(minutes=10))
    card = _card(
        status="review",
        gate=True,
        events=(event, _event("created-1", "created", NOW - timedelta(hours=2))),
        comments=(comment,),
    )
    decision = watch(_input(config, FakeBoard((card,))))
    assert "gate_action" in _ids(decision)
    done = Card(**{**card.__dict__, "status": "done", "done_at": NOW - timedelta(minutes=5)})
    assert "gate_action" not in _ids(watch(_input(config, FakeBoard((done,)))))
    ignored = Card(**{**card.__dict__, "ignored_event_ids": ("ev-1",)})
    assert "gate_action" not in _ids(watch(_input(config, FakeBoard((ignored,)))))
    recent = Card(
        **{
            **card.__dict__,
            "events": (_event("ev-1", "review_requested", NOW - timedelta(minutes=5)),),
        }
    )
    assert "gate_action" not in _ids(watch(_input(config, FakeBoard((recent,)))))


def test_reject_comment_without_reopen_warns_and_a_real_start_does_not(tmp_path):
    config = _config(tmp_path)
    event = _event("ev-1", "review_requested", NOW - HOUR)
    comment = Comment("handled event=ev-1 action=reject", NOW - timedelta(minutes=20))
    card = _card(status="review", gate=True, events=(event,), comments=(comment,))
    assert "gate_action" in _ids(watch(_input(config, FakeBoard((card,)))))
    started = Card(
        **{
            **card.__dict__,
            "events": (event, _event("re-1", "review_reopened", NOW - timedelta(minutes=15))),
            "runs": (Run("run-2", NOW - timedelta(minutes=14), "exited"),),
        }
    )
    assert "gate_action" not in _ids(watch(_input(config, FakeBoard((started,)))))


def test_wait_is_valid_only_while_the_reason_is_open(tmp_path):
    config = _config(tmp_path)
    event = _event("ev-1", "review_requested", NOW - HOUR)
    comment = Comment("handled event=ev-1 action=wait:line-1", NOW - timedelta(minutes=5))
    card = _card(status="review", gate=True, events=(event,), comments=(comment,))
    assert "gate_action" in _ids(watch(_input(config, FakeBoard((card,)))))
    waiting = watch(_input(config, FakeBoard((card,), waits=("line-1",))))
    assert "gate_action" not in _ids(waiting)
    blocked = Card(**{**card.__dict__, "status": "blocked"})
    assert "gate_action" not in _ids(watch(_input(config, FakeBoard((blocked,)))))


def test_reject_followup_deadline_running_and_new_review(tmp_path):
    config = _config(tmp_path)
    reopen = _event("re-1", "review_reopened", NOW - timedelta(hours=2))
    card = _card(
        status="ready",
        gate=True,
        time_limit_seconds=0,
        events=(reopen,),
        runs=(Run("run-2", NOW - timedelta(hours=2), "exited"),),
    )
    assert "reject_followup" in _ids(watch(_input(config, FakeBoard((card,)))))
    early_at = NOW - timedelta(hours=2) + timedelta(minutes=30)
    early = watch(_input(config, FakeBoard((card,)), now=early_at))
    assert "reject_followup" not in _ids(early)
    running = Card(
        **{**card.__dict__, "runs": (Run("run-2", NOW - timedelta(minutes=5), "running"),)}
    )
    assert "reject_followup" not in _ids(watch(_input(config, FakeBoard((running,)))))
    reviewed = Card(
        **{
            **card.__dict__,
            "events": (reopen, _event("ev-2", "review_requested", NOW - timedelta(minutes=5))),
        }
    )
    assert "reject_followup" not in _ids(watch(_input(config, FakeBoard((reviewed,)))))


def test_next_card_must_start_within_the_configured_time(tmp_path):
    config = _config(tmp_path)
    comment = Comment("handled event=ev-1 action=next:card-2", NOW - HOUR)
    parent = _card(comments=(comment,))
    missing = watch(_input(config, FakeBoard((parent,))))
    assert "next_not_started" in _ids(missing)
    child = _card("card-2", runs=(Run("run-1", NOW - timedelta(minutes=50), "running"),))
    assert "next_not_started" not in _ids(watch(_input(config, FakeBoard((parent, child)))))
    late = _card("card-2", runs=(Run("run-1", NOW, "running"),))
    assert "next_not_started" in _ids(watch(_input(config, FakeBoard((parent, late)))))
    young = Comment("handled event=ev-1 action=next:card-2", NOW - timedelta(minutes=5))
    waiting = _card(comments=(young,))
    assert "next_not_started" not in _ids(watch(_input(config, FakeBoard((waiting,)))))


@pytest.mark.parametrize(
    "status",
    ["blocked", "triage", "crashed", "gave_up", "timed_out", "block_loop_detected"],
)
def test_intermediate_failure_statuses_warn_and_gate_cards_do_not(tmp_path, status):
    config = _config(tmp_path)
    card = _card(status=status)
    assert "intermediate_failure" in _ids(watch(_input(config, FakeBoard((card,)))))
    gate_card = _card(status=status, gate=True, subscriptions=("main.invalid",))
    assert "intermediate_failure" not in _ids(watch(_input(config, FakeBoard((gate_card,)))))


def test_unstarted_card_after_parents_are_done(tmp_path):
    config = _config(tmp_path)
    parent = _card("parent-1", status="done", done_at=NOW - timedelta(hours=2))
    child = _card(status="todo", parents=("parent-1",))
    assert "unstarted_after_parents" in _ids(watch(_input(config, FakeBoard((parent, child)))))
    recent = Card(**{**parent.__dict__, "done_at": NOW - timedelta(minutes=5)})
    assert "unstarted_after_parents" not in _ids(watch(_input(config, FakeBoard((recent, child)))))
    missing = _card(status="todo", parents=("missing",))
    decision = watch(_input(config, FakeBoard((missing,))))
    assert any(cause.uncheckable for cause in decision.causes)


def test_start_approval_without_card_or_link(tmp_path):
    config = _config(tmp_path)
    old = _start(at=NOW - HOUR)
    assert "start_without_card" in _ids(watch(_input(config, FakeBoard(), approvals=(old,))))
    young = _start(at=NOW - timedelta(minutes=5))
    assert "start_without_card" not in _ids(watch(_input(config, FakeBoard(), approvals=(young,))))
    card = _card(approval_key="apr-1")
    no_link = watch(_input(config, FakeBoard((card,)), approvals=(old,)))
    assert any(cause.cause_id.endswith(":no-link") for cause in no_link.causes)
    link = Approval(RECORD_VERSION, "link", "apr-1", card_id="missing")
    missing = watch(_input(config, FakeBoard(), approvals=(young, link)))
    assert any(cause.cause_id.endswith(":missing-card") for cause in missing.causes)
    linked = Approval(RECORD_VERSION, "link", "apr-1", card_id="card-1")
    quiet = watch(_input(config, FakeBoard((card,)), approvals=(old, linked)))
    assert "start_without_card" not in _ids(quiet)


def test_missing_gate_subscription_warns(tmp_path):
    config = _config(tmp_path)
    bare = _card(gate=True, status="review")
    assert "gate_subscription" in _ids(watch(_input(config, FakeBoard((bare,)))))
    subscribed = Card(**{**bare.__dict__, "subscriptions": ("main.invalid",)})
    assert "gate_subscription" not in _ids(watch(_input(config, FakeBoard((subscribed,)))))
    other = _card(status="blocked")
    assert "gate_subscription" not in _ids(watch(_input(config, FakeBoard((other,)))))


def test_done_with_failing_checklist_warns_and_a_pass_does_not(tmp_path):
    config = _config(tmp_path)
    bad = _checklist(tmp_path / "bad.json", False)
    good = _checklist(tmp_path / "good.json", True)
    failed = _card(status="done", checklist_path=str(bad))
    assert "checklist_done" in _ids(watch(_input(config, FakeBoard((failed,)))))
    passed = _card(status="done", checklist_path=str(good), fingerprint="fp-1", basis="basis-1")
    decision = watch(_input(config, FakeBoard((passed,)), reviews=(_pass(),)))
    assert "checklist_done" not in _ids(decision)
    reviewing = _card(status="review", checklist_path=str(bad))
    assert "checklist_done" not in _ids(watch(_input(config, FakeBoard((reviewing,)))))


def test_reject_cap_warns_only_when_the_gate_is_still_in_review(tmp_path):
    config = _config(tmp_path)
    capped = _card(status="review", gate=True, reject_count=3, subscriptions=("main.invalid",))
    assert "reject_cap" in _ids(watch(_input(config, FakeBoard((capped,)))))
    under = Card(**{**capped.__dict__, "reject_count": 2})
    assert "reject_cap" not in _ids(watch(_input(config, FakeBoard((under,)))))
    done = Card(**{**capped.__dict__, "status": "done"})
    assert "reject_cap" not in _ids(watch(_input(config, FakeBoard((done,)))))


def test_stall_is_unchanged_files_not_a_failure_status(tmp_path):
    config = _config(tmp_path)
    output = tmp_path / "out.txt"
    output.write_text("same\n", encoding="utf-8")
    old = (NOW - timedelta(hours=2)).timestamp()
    output.touch()
    import os

    os.utime(output, (old, old))
    running = _card(
        status="running",
        output_paths=(str(output),),
        runs=(Run("run-1", NOW - timedelta(hours=2), "running"),),
    )
    assert "stalled" in _ids(watch(_input(config, FakeBoard((running,)))))
    fresh = (NOW - timedelta(minutes=1)).timestamp()
    os.utime(output, (fresh, fresh))
    assert "stalled" not in _ids(watch(_input(config, FakeBoard((running,)))))
    blocked = Card(**{**running.__dict__, "status": "blocked"})
    os.utime(output, (old, old))
    assert "stalled" not in _ids(watch(_input(config, FakeBoard((blocked,)))))


def test_done_without_pass_warns_and_a_matching_pass_does_not(tmp_path):
    config = _config(tmp_path)
    checklist = str(_checklist(tmp_path / "ok.json", True))
    card = _card(status="done", fingerprint="fp-1", basis="basis-1", checklist_path=checklist)
    assert "unchecked_result" in _ids(watch(_input(config, FakeBoard((card,)))))
    quiet = watch(_input(config, FakeBoard((card,)), reviews=(_pass(),)))
    assert "unchecked_result" not in _ids(quiet)
    reviewing = Card(**{**card.__dict__, "status": "review"})
    assert "unchecked_result" not in _ids(watch(_input(config, FakeBoard((reviewing,)))))


def test_applied_fingerprint_must_match_the_approval(tmp_path):
    config = _config(tmp_path)
    card = _card(applied_fingerprint="fp-2")
    approval = _apply()
    watched = watch(_input(config, FakeBoard((card,)), approvals=(approval,)))
    assert "applied_differs" in _ids(watched)
    same = Card(**{**card.__dict__, "applied_fingerprint": "fp-1"})
    quiet = watch(_input(config, FakeBoard((same,)), approvals=(approval,)))
    assert "applied_differs" not in _ids(quiet)


def test_rewarn_interval_grows_and_user_notice_is_capped(tmp_path):
    config = _config(tmp_path)
    cause = "card-1:ev-1"

    def note(at, channel="internal", summary="no action"):
        return Warning(RECORD_VERSION, cause, at, channel, summary)

    def internal(history, at):
        return internal_rewarn_due(history, cause, at, config)

    def user(history, at, received, summary="no action"):
        return user_notify_due(history, cause, at, config, received, summary)

    first = note(NOW)
    assert internal((), NOW)
    assert not internal((first,), NOW + timedelta(seconds=1799))
    assert internal((first,), NOW + timedelta(seconds=1800))
    second = note(NOW + timedelta(seconds=1800))
    assert not internal((first, second), second.warned_at + timedelta(seconds=3599))
    assert internal((first, second), second.warned_at + timedelta(seconds=3600))
    history = (first, second, note(NOW + timedelta(hours=2)))
    assert not internal(history, NOW + timedelta(days=1))
    assert not user((), NOW, None)
    assert not user((first,), NOW + timedelta(seconds=599), None)
    assert user((first,), NOW + timedelta(seconds=600), None)
    assert not user((first,), NOW + timedelta(seconds=600), NOW)
    assert user(history, NOW, NOW)
    told = (*history, note(NOW, "user"))
    assert not user(told, NOW + timedelta(days=1), None)
    assert user(told, NOW + timedelta(days=1), None, "status changed")
    comments = (Comment("received alert=card-1:ev-1", NOW),)
    assert received_at(comments, config.comments, cause) == NOW
    assert received_at((), config.comments, cause) is None


def test_running_parent_is_not_an_unreadable_done_time(tmp_path):
    config = _config(tmp_path)
    parent = _card("parent-1", status="running")
    child = _card(status="todo", parents=("parent-1",))
    assert "unstarted_after_parents" not in _ids(watch(_input(config, FakeBoard((parent, child)))))
    done = _card("parent-1", status="done", done_at=None)
    decision = watch(_input(config, FakeBoard((done, child))))
    assert any(cause.uncheckable for cause in decision.causes)


def test_new_run_does_not_inherit_an_old_stall_clock(tmp_path):
    config = _config(tmp_path)
    output = tmp_path / "out.txt"
    output.write_text("same\n", encoding="utf-8")
    old = (NOW - timedelta(hours=2)).timestamp()
    os.utime(output, (old, old))
    running = _card(
        status="running",
        output_paths=(str(output),),
        runs=(
            Run("run-old", NOW - timedelta(hours=3), "done"),
            Run("run-new", NOW - timedelta(seconds=5), "running"),
        ),
    )
    assert "stalled" not in _ids(watch(_input(config, FakeBoard((running,)))))
    bare = _card(
        status="running",
        runs=(
            Run("run-old", NOW - timedelta(hours=3), "done"),
            Run("run-new", NOW - timedelta(seconds=5), "running"),
        ),
    )
    assert "stalled" not in _ids(watch(_input(config, FakeBoard((bare,)))))


def test_read_error_failure_is_a_decision(tmp_path):
    class _Raising:
        def read_error(self):
            raise BoardReadError("board error unread")

        def cards(self):
            return ()

        def card(self, _card_id):
            return None

        def open_wait_ids(self):
            return ()

    config = _config(tmp_path)
    board = _Raising()
    watched = watch(_input(config, board))
    assert not watched.block
    assert watched.causes[0].uncheckable
    blocked = gate(
        "complete",
        _input(config, board, card_id="card-1"),
        profile_in_list=True,
        board_matches=True,
    )
    assert blocked.block
    assert blocked.causes[0].uncheckable
