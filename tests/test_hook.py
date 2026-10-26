"""The dispatch hook is off unless watch_hook is true, then it uses scan."""

import json
from datetime import datetime, timezone

from taskwatch.config import ConfigError, config_path, load_config
from taskwatch.hook import on_dispatch_tick
from taskwatch.protocol import Card
from tests.config_fixture import config_dict, write_config
from tests.fake_board import FakeBoard
from tests.test_cli import Writer, _Linked, _refuse

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


def _root_config(tmp_path, **updates):
    root = tmp_path / "hermes"
    raw = config_dict()
    raw["hermes_root"] = str(root)
    raw.update(updates)
    write_config(config_path(root), raw)
    return root


def test_missing_watch_hook_is_refused(tmp_path):
    raw = config_dict()
    del raw["watch_hook"]
    write_config(tmp_path / "config.json", raw)
    try:
        load_config(tmp_path / "config.json")
    except ConfigError as exc:
        assert "watch_hook" in str(exc)
    else:
        raise AssertionError("missing watch_hook was accepted")


def test_string_false_is_not_off(tmp_path):
    raw = config_dict()
    raw["watch_hook"] = "false"
    write_config(tmp_path / "config.json", raw)
    try:
        load_config(tmp_path / "config.json")
    except ConfigError as exc:
        assert "boolean" in str(exc)
    else:
        raise AssertionError("string false was accepted")


def test_hook_stays_quiet_when_off_or_uninitialized(tmp_path):
    root = _root_config(tmp_path, watch_hook=False)
    board = FakeBoard((Card(id="c1", status="blocked"),))
    writer = Writer()
    assert (
        on_dispatch_tick(
            root=root,
            board_name="board.invalid",
            profile_name="main",
            reader=board,
            writer=writer,
            now=NOW,
            deliver=_refuse,
        )
        == ()
    )
    assert writer.cards == {}
    assert (
        on_dispatch_tick(
            root=tmp_path / "empty",
            board_name="board.invalid",
            profile_name="main",
            reader=board,
            writer=writer,
            now=NOW,
            deliver=_refuse,
        )
        == ()
    )


def test_hook_uses_the_scan_path_when_enabled(tmp_path):
    root = _root_config(tmp_path, watch_hook=True)
    work = Card(id="c1", status="blocked")
    writer = Writer()
    board = _Linked((work,), writer)
    on_dispatch_tick(
        root=root,
        board_name="board.invalid",
        profile_name="main",
        reader=board,
        writer=writer,
        now=NOW,
        deliver=_refuse,
    )
    assert writer.cards["w1"]["status"] == "done"
    assert board.card("c1").status == "blocked"
    again = Writer()
    on_dispatch_tick(
        root=root,
        board_name="other.invalid",
        profile_name="main",
        reader=_Linked((work,), again),
        writer=again,
        now=NOW,
        deliver=_refuse,
    )
    assert "target" in again.cards["w1"]["body"]
    assert "blocked" not in again.cards["w1"]["title"]


def test_init_draft_leaves_the_hook_off(tmp_path):
    from taskwatch.commands import init_layout

    root = tmp_path / "hermes"
    init_layout(
        root,
        board="board.invalid",
        profiles=("main",),
        hermes_exe="/placeholder/hermes",
        unit_dir=tmp_path / "units",
        runner=lambda argv: None,
    )
    raw = json.loads(config_path(root).read_text(encoding="utf-8"))
    assert raw["watch_hook"] is False
    assert raw["version"] == 1


def test_dispatch_tick_opens_the_live_board_when_none_is_injected(tmp_path, monkeypatch):
    from taskwatch.hook import dispatch_tick

    root = _root_config(tmp_path, watch_hook=False)
    seen = {}

    def fake_board(slug=None, db_path=None):
        del db_path
        seen["slug"] = slug
        return FakeBoard()

    monkeypatch.setattr("taskwatch.hermes_board.open_board", fake_board)
    assert dispatch_tick(board="board.invalid", profile_name="main", root=root, now=NOW) == ()
    assert seen["slug"] == "board.invalid"


def test_dispatch_tick_checks_the_live_result_not_the_card_body(tmp_path, monkeypatch):
    from taskwatch.hook import dispatch_tick

    root = _root_config(tmp_path, watch_hook=True)
    writer = Writer()
    reader = _Linked((Card(id="c1", status="done", fingerprint="f" * 40, basis="b" * 64),), writer)

    class Live:
        cards = reader.cards
        card = reader.card
        open_wait_ids = reader.open_wait_ids
        read_error = reader.read_error
        create_blocked = writer.create_blocked
        subscribe = writer.subscribe
        subscribed = writer.subscribed
        complete = writer.complete
        archive = writer.archive
        statuses = writer.statuses

    monkeypatch.setattr("taskwatch.hermes_board.open_board", lambda slug=None, db_path=None: Live)
    dispatch_tick(
        board=load_config(config_path(root)).board,
        profile_name="main",
        root=root,
        now=NOW,
        deliver=_refuse,
    )
    # With no run record the card has no fingerprint, whatever its body says.
    assert any("no fingerprint or basis" in item["body"] for item in writer.cards.values())
