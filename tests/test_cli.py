"""CLI rules. A fake board and a temp directory stand in for Hermes."""

import argparse
import json
from datetime import datetime, timedelta, timezone

import pytest

from taskwatch.card_body import loads
from taskwatch.checks import WatchTarget
from taskwatch.cli import handle_cli, setup_cli
from taskwatch.commands import CommandError, Probe, init_layout, scan_board
from taskwatch.config import config_path, load_config
from taskwatch.protocol import Card, Comment
from taskwatch.state import State
from tests.config_fixture import config_dict
from tests.fake_board import FakeBoard

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)
HERMES = "/placeholder/hermes"


def _refuse(destination: str, text: str) -> None:
    raise AssertionError(f"no user line expected: {text}")


class Writer:
    def __init__(self, fail_subscribe: bool = False):
        self.cards: dict[str, dict] = {}
        self.subs: dict[str, set[str]] = {}
        self.fail_subscribe = fail_subscribe

    def create_blocked(self, title: str, body: str) -> str:
        card_id = f"w{len(self.cards) + 1}"
        self.cards[card_id] = {"title": title, "body": body, "status": "blocked"}
        return card_id

    def subscribe(self, card_id: str, destination: str) -> None:
        if self.fail_subscribe:
            return
        self.subs.setdefault(card_id, set()).add(destination)

    def subscribed(self, card_id: str, destination: str) -> bool:
        return destination in self.subs.get(card_id, set())

    def complete(self, card_id: str, summary: str) -> bool:
        self.cards[card_id]["status"] = "done"
        self.cards[card_id]["summary"] = summary
        return True

    def archive(self, card_id: str) -> None:
        self.cards[card_id]["status"] = "archived"

    def statuses(self) -> dict[str, str]:
        return {card_id: item["status"] for card_id, item in self.cards.items()}


def _spec() -> str:
    return json.dumps(
        {
            "platform": "placeholder",
            "chat_id": "chat.invalid",
            "thread_id": "0",
            "chat_type": "dm",
            "profile": "main",
            "delivery_mode": "wake",
        },
        separators=(",", ":"),
    )


def _write_ready(root, raw: dict | None = None) -> None:
    body = raw if raw is not None else config_dict()
    body = json.loads(json.dumps(body))
    body["hermes_root"] = str(root)
    body["destinations"] = {"main_subscription": _spec(), "user_notify": _spec()}
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


def _parser():
    parser = argparse.ArgumentParser()
    setup_cli(parser)
    return parser


def test_init_writes_a_timer_argument_list_and_refuses_a_second_write(tmp_path):
    calls = []
    root = tmp_path / "hermes"
    units = tmp_path / "units"
    path = init_layout(
        root,
        board="board.invalid",
        profiles=("main",),
        hermes_exe=HERMES,
        unit_dir=units,
        runner=calls.append,
    )
    service = (units / "hermes-taskwatch-scan.service").read_text(encoding="utf-8")
    assert f"ExecStart={HERMES} taskwatch scan\n" in service
    assert "sh -c" not in service
    assert calls == [["systemctl", "--user", "daemon-reload"]]
    assert "enable" not in calls[0]
    assert load_config(path).board == "board.invalid"
    with pytest.raises(CommandError, match="already exists"):
        init_layout(
            root,
            board="board.invalid",
            profiles=("main",),
            hermes_exe=HERMES,
            unit_dir=units,
            runner=calls.append,
        )


def test_doctor_fails_when_a_listed_profile_has_the_plugin_off(tmp_path):
    root = tmp_path / "hermes"
    _write_ready(root)
    probe = Probe("0.21.5", True, lambda profile: profile != "worker")
    with pytest.raises(SystemExit) as caught:
        handle_cli(
            _parser().parse_args(["doctor", "--root", str(root)]),
            {"board": FakeBoard(), "probe": probe},
        )
    assert caught.value.code == 1


def test_doctor_fails_on_a_version_mismatch(tmp_path):
    root = tmp_path / "hermes"
    _write_ready(root)
    probe = Probe("0.20.1", True, lambda profile: True)
    with pytest.raises(SystemExit) as caught:
        handle_cli(
            _parser().parse_args(["doctor", "--root", str(root)]),
            {"board": FakeBoard(), "probe": probe},
        )
    assert caught.value.code == 1


def test_doctor_warns_above_the_tested_version_and_does_not_fail(tmp_path):
    root = tmp_path / "hermes"
    _write_ready(root)
    probe = Probe("0.22.0", True, lambda profile: True)
    text = handle_cli(
        _parser().parse_args(["doctor", "--root", str(root)]),
        {"board": FakeBoard(), "probe": probe},
    )
    assert "hermes: warn" in text
    assert "0.22.0" in text
    later = Probe("0.21.10", True, lambda profile: True)
    text = handle_cli(
        _parser().parse_args(["doctor", "--root", str(root)]),
        {"board": FakeBoard(), "probe": later},
    )
    assert "hermes: warn" in text


def test_doctor_and_scan_open_the_live_board_when_none_is_injected(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    _write_ready(root)
    seen = {}

    def fake_board(slug=None, db_path=None):
        del db_path
        seen["slug"] = slug
        return _Live()

    def fake_probe(path):
        seen["probe"] = path
        return Probe("0.21.5", True, lambda profile: True)

    monkeypatch.setattr("taskwatch.hermes_board.open_board", fake_board)
    monkeypatch.setattr("taskwatch.hermes_board.live_probe", fake_probe)
    text = handle_cli(_parser().parse_args(["doctor", "--root", str(root)]), {})
    assert "hermes: ok" in text
    assert seen["slug"] == "board.invalid"
    handle_cli(_parser().parse_args(["scan", "--root", str(root)]), {"profile": "main"})
    assert seen["slug"] == "board.invalid"


class _Live(FakeBoard):
    def statuses(self):
        return {}

    def create_blocked(self, title, body):
        raise AssertionError(body or title)


def test_scan_warns_on_target_mismatch_and_leaves_the_work_card(tmp_path):
    root = tmp_path / "hermes"
    _write_ready(root)
    card = Card(id="c1", status="done", checklist_path=None)
    writer = Writer()
    board = _Linked((card,), writer)
    config = load_config(config_path(root))
    scan_board(
        config,
        board,
        State(root),
        writer,
        NOW,
        WatchTarget("/other/hermes", config.board, "main"),
        deliver=_refuse,
    )
    assert card.status == "done"
    assert len(writer.cards) == 1
    assert "target" in writer.cards["w1"]["body"]
    assert "checklist" not in writer.cards["w1"]["body"]


def test_scan_does_not_write_a_second_warning_before_the_backoff(tmp_path):
    root = tmp_path / "hermes"
    _write_ready(root)
    writer = Writer()
    board = _Linked((Card(id="c1", status="blocked"),), writer)
    config = load_config(config_path(root))
    target = WatchTarget(str(root), config.board, "main")
    scan_board(config, board, State(root), writer, NOW, target, deliver=_refuse)
    scan_board(
        config, board, State(root), writer, NOW + timedelta(seconds=1), target, deliver=_refuse
    )
    assert list(writer.statuses()) == ["w1"]
    assert board.card("c1").status == "blocked"


def test_scan_archives_a_warning_when_the_subscription_is_missing(tmp_path):
    root = tmp_path / "hermes"
    _write_ready(root)
    board = FakeBoard((Card(id="c1", status="blocked"),))
    writer = Writer(fail_subscribe=True)
    config = load_config(config_path(root))
    scan_board(
        config,
        board,
        State(root),
        writer,
        NOW,
        WatchTarget(str(root), config.board, "main"),
        deliver=_refuse,
    )
    assert writer.statuses()["w1"] == "archived"
    assert State(root).load().warnings == ()


def test_check_is_read_only_and_blocks_a_failing_checklist(tmp_path):
    root = tmp_path / "hermes"
    _write_ready(root)
    checklist = tmp_path / "check.json"
    checklist.write_text('{"items":[{"id":"a","passes":false}]}', encoding="utf-8")
    card = Card(id="c1", status="running", exit_code=0, checklist_path=str(checklist))
    board = FakeBoard((card,))
    with pytest.raises(SystemExit) as caught:
        handle_cli(
            _parser().parse_args(["check", "c1", "--root", str(root)]),
            {"board": board, "now": NOW},
        )
    assert caught.value.code == 1
    assert board.card("c1").status == "running"


class _Linked:
    """Reader that shows the work cards plus the warning cards the writer made."""

    def __init__(self, base: tuple[Card, ...], writer: Writer):
        self.base = base
        self.writer = writer

    def cards(self) -> tuple[Card, ...]:
        made = []
        for card_id, item in self.writer.cards.items():
            if item["status"] == "archived":
                continue
            made.append(
                Card(
                    id=card_id,
                    status=item["status"],
                    warning_for=loads(item["body"]).get("warning_for"),
                    subscriptions=tuple(self.writer.subs.get(card_id, ())),
                    comments=tuple(item.get("comments", ())),
                    done_at=NOW if item["status"] == "done" else None,
                )
            )
        return self.base + tuple(made)

    def card(self, card_id: str) -> Card | None:
        return next((item for item in self.cards() if item.id == card_id), None)

    def open_wait_ids(self) -> tuple[str, ...]:
        return ()

    def read_error(self) -> str | None:
        return None


def _scanning(tmp_path, **times):
    root = tmp_path / "hermes"
    raw = config_dict()
    raw["times"].update(times)
    _write_ready(root, raw)
    config = load_config(config_path(root))
    writer = Writer()
    board = _Linked((Card(id="c1", status="blocked"),), writer)
    target = WatchTarget(str(root), config.board, "main")

    def scan(seconds: int, deliver=_refuse):
        return scan_board(
            config,
            board,
            State(root),
            writer,
            NOW + timedelta(seconds=seconds),
            target,
            deliver=deliver,
        )

    return root, config, writer, scan


def test_scan_does_not_warn_about_its_own_warning_cards(tmp_path):
    _, _, writer, scan = _scanning(tmp_path, rewarn_initial_seconds=1)
    for seconds in (0, 1, 3):
        scan(seconds, lambda destination, text: None)
    assert [item["title"] for item in writer.cards.values()] == ["c1:blocked"] * 3


def test_user_line_is_not_recorded_until_delivery_succeeds(tmp_path):
    root, config, _, scan = _scanning(
        tmp_path, rewarn_initial_seconds=100, user_notify_unreceived_seconds=10
    )
    scan(0)

    def down(destination, text):
        raise OSError("platform down")

    assert scan(11, down) == ("undelivered c1:blocked: platform down",)
    assert [item.channel for item in State(root).load().warnings] == ["internal"]
    sent = []
    lines = scan(12, lambda destination, text: sent.append((destination, text)))
    assert lines == (f"{config.destinations.user_notify} c1:blocked",)
    assert sent[0][0] == config.destinations.user_notify
    assert "c1:blocked" in sent[0][1]
    assert [item.channel for item in State(root).load().warnings] == ["internal", "user"]
    assert scan(13) == ()


def test_user_notice_does_not_wait_for_the_rewarn_interval(tmp_path):
    _, _, _, scan = _scanning(
        tmp_path, rewarn_initial_seconds=100, user_notify_unreceived_seconds=10
    )
    scan(0)
    assert scan(5) == ()
    sent = []
    scan(11, lambda destination, text: sent.append(text))
    assert len(sent) == 1


def test_received_warning_is_not_sent_to_the_user(tmp_path):
    _, _, writer, scan = _scanning(
        tmp_path, rewarn_initial_seconds=100, user_notify_unreceived_seconds=10
    )
    scan(0)
    writer.cards["w1"]["comments"] = [Comment("received alert=c1:blocked", NOW)]
    assert scan(11) == ()


def test_a_reply_to_an_earlier_warning_does_not_hide_an_unanswered_new_one(tmp_path):
    _, _, writer, scan = _scanning(
        tmp_path, rewarn_initial_seconds=20, user_notify_unreceived_seconds=10
    )
    scan(0)
    writer.cards["w1"]["comments"] = [Comment("received alert=c1:blocked", NOW)]
    scan(21)
    assert sorted(writer.cards) == ["w1", "w2"]
    assert scan(25) == ()
    sent = []
    scan(32, lambda destination, text: sent.append(text))
    assert len(sent) == 1


def test_a_late_reply_on_an_older_warning_does_not_hide_the_unanswered_latest_one(tmp_path):
    _, _, writer, scan = _scanning(
        tmp_path, rewarn_initial_seconds=20, user_notify_unreceived_seconds=10
    )
    scan(0)
    scan(21)
    assert sorted(writer.cards) == ["w1", "w2"]
    late = NOW + timedelta(seconds=22)
    writer.cards["w1"]["comments"] = [Comment("received alert=c1:blocked", late)]
    sent = []
    scan(32, lambda destination, text: sent.append(text))
    assert len(sent) == 1


def test_a_reply_on_the_latest_warning_keeps_the_user_out_of_it(tmp_path):
    _, _, writer, scan = _scanning(
        tmp_path, rewarn_initial_seconds=20, user_notify_unreceived_seconds=10
    )
    scan(0)
    scan(21)
    late = NOW + timedelta(seconds=22)
    writer.cards["w2"]["comments"] = [Comment("received alert=c1:blocked", late)]
    assert scan(32) == ()


def test_a_whole_second_reply_counts_for_a_warning_stamped_inside_that_second(tmp_path):
    _, _, writer, scan = _scanning(
        tmp_path, rewarn_initial_seconds=100, user_notify_unreceived_seconds=10
    )
    scan(0.7)
    writer.cards["w1"]["comments"] = [Comment("received alert=c1:blocked", NOW)]
    assert scan(12) == ()


def test_deleted_warning_subscription_is_sent_to_the_user_at_once(tmp_path):
    _, _, writer, scan = _scanning(
        tmp_path, rewarn_initial_seconds=100, user_notify_unreceived_seconds=1000
    )
    scan(0)
    writer.subs["w1"].clear()
    sent = []
    scan(1, lambda destination, text: sent.append(text))
    assert len(sent) == 1


def test_doctor_reports_an_unreadable_config_instead_of_crashing(tmp_path):
    root = tmp_path / "hermes"
    root.mkdir()
    probe = Probe("0.21.5", True, lambda profile: True)
    with pytest.raises(SystemExit) as caught:
        handle_cli(
            _parser().parse_args(["doctor", "--root", str(root)]),
            {"board": FakeBoard(), "probe": probe},
        )
    assert caught.value.code == 1
