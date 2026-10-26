"""Create refuses a missing field. Reject is idempotent, serialized, and capability-checked."""

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from taskwatch.cards import create_cards, reject_gate
from taskwatch.config import load_config
from taskwatch.state import State
from tests.config_fixture import write_config
from tests.threads_fixture import in_threads

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


class Board:
    def __init__(self, find_delay: float = 0.0):
        self.cards = {}
        self.subs = {}
        self.calls = []
        self.archived = []
        self.reopened = []
        self.reopen_delay = 0.0
        self.find_delay = find_delay
        self.lock = threading.Lock()

    def find_approval(self, key):
        found = None
        for card_id, card in list(self.cards.items()):
            if key in card["body"] and card["status"] != "archived":
                found = card_id
        time.sleep(self.find_delay)
        return found

    def create(self, title, body, parents=(), blocked=False):
        with self.lock:
            card_id = f"c{len(self.cards) + 1}"
            self.cards[card_id] = {
                "title": title,
                "body": body,
                "parents": parents,
                "status": "blocked" if blocked else "todo",
            }
        return card_id

    def subscribe(self, card_id, destination):
        self.subs.setdefault(card_id, set()).add(destination)

    def subscribed(self, card_id, destination):
        return destination in self.subs.get(card_id, set())

    def unblock(self, card_id):
        self.cards[card_id]["status"] = "ready"

    def archive(self, card_id):
        self.archived.append(card_id)
        self.cards[card_id]["status"] = "archived"

    def gate_role(self, card_id):
        return self.cards[card_id]["role"]

    def waiting(self, card_id):
        return self.cards[card_id]["status"] == "review"

    def event_mark(self, card_id):
        return max(self.reopened, default=0)

    def reopened_after(self, card_id, mark):
        return any(item > mark for item in self.reopened)

    def _reopen(self, kind, card_id, text):
        time.sleep(self.reopen_delay)
        self.calls.append((kind, card_id, text))
        self.cards[card_id]["status"] = "ready"
        self.reopened.append(max(self.reopened, default=0) + 1)

    def reject_worker(self, card_id, text):
        self._reopen("worker", card_id, text)


def _ready(tmp_path):
    root = tmp_path / "hermes"
    root.mkdir()
    config = load_config(write_config(root / "taskwatch" / "config.json"))
    state = State(root)
    state.append_start("apr-1", NOW, "start the work")
    intent = tmp_path / "intent.md"
    spec = tmp_path / "spec.md"
    intent.write_text(
        "## Source\nuser\n## Purpose\nwhy\n## Done\nwhen\n## Approval\napr-1\n",
        encoding="utf-8",
    )
    spec.write_text(
        "## Outputs\nout\n## Checklist\nlist.json\n## Out of scope\nnone\n"
        "## Live\npath.invalid\n## Guidance\nnone\n## Model\nexample\n",
        encoding="utf-8",
    )
    return config, state, intent, spec


def _create(tmp_path, board, cards=({"title": "work", "gate": True},), state=None, **extra):
    config, own_state, intent, spec = extra.pop("ready", None) or _ready(tmp_path)
    return create_cards(
        config,
        state or own_state,
        board,
        approval_key="apr-1",
        intent=intent,
        spec=spec,
        cards=cards,
        now=NOW,
    )


def _reject(config, state, board, reject_id="r1", card_id="g1", at=NOW):
    return reject_gate(
        config,
        state,
        board,
        card_id=card_id,
        reason="fix it",
        reject_id=reject_id,
        now=at,
    )


def _gate(role="worker", status="review"):
    return {"role": role, "status": status, "body": ""}


def test_missing_intent_field_creates_no_card(tmp_path):
    ready = _ready(tmp_path)
    ready[2].write_text("## Source\nuser\n", encoding="utf-8")
    board = Board()
    result = _create(tmp_path, board, ready=ready)
    assert result["ok"] is False
    assert board.cards == {}


def test_create_links_and_a_second_call_does_not_make_another_card(tmp_path):
    ready = _ready(tmp_path)
    board = Board()
    first = _create(tmp_path, board, ready=ready)
    assert first["ok"] is True
    assert "apr-1" in board.cards[first["card_id"]]["body"]
    assert board.cards[first["card_id"]]["status"] == "ready"
    second = _create(tmp_path, board, ready=ready)
    assert second["duplicate"] is True
    assert len(board.cards) == 1


def test_two_calls_for_one_approval_make_one_card(tmp_path):
    ready = _ready(tmp_path)
    board = Board(find_delay=0.3)
    results = []

    def call(delay):
        time.sleep(delay)
        # A second process has its own State object over the same files.
        results.append(_create(tmp_path, board, state=State(tmp_path / "hermes"), ready=ready))

    threads = [threading.Thread(target=call, args=(delay,)) for delay in (0, 0.1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    live = [card for card in board.cards.values() if card["status"] != "archived"]
    assert len(live) == 1
    assert all(item["ok"] for item in results)
    assert {item["card_id"] for item in results} == {"c1"}


def test_recovery_links_the_existing_card_and_does_not_create_another(tmp_path):
    ready = _ready(tmp_path)
    board = Board()
    board.cards["c9"] = {"body": "approval_key: apr-1", "status": "blocked", "role": "worker"}
    result = _create(tmp_path, board, ready=ready)
    assert result["recovered"] is True
    assert result["card_id"] == "c9"
    assert len(board.cards) == 1
    assert ready[1].load().approvals[-1].kind == "link"


def test_subscription_failure_archives_children_before_their_parents(tmp_path):
    board = Board()
    board.subscribe = lambda card_id, destination: None
    cards = (
        {"title": "root"},
        {"title": "middle", "parents": (0,)},
        {"title": "gate", "parents": (1,), "gate": True},
    )
    result = _create(tmp_path, board, cards=cards)
    assert result["ok"] is False
    assert board.archived == ["c3", "c2", "c1"]
    assert {card["status"] for card in board.cards.values()} == {"archived"}


def test_rollback_that_cannot_archive_a_child_leaves_its_parents_alone(tmp_path):
    board = Board()
    board.subscribe = lambda card_id, destination: None

    def stuck(card_id):
        raise OSError("busy")

    board.archive = stuck
    cards = ({"title": "root"}, {"title": "gate", "parents": (0,), "gate": True})
    result = _create(tmp_path, board, cards=cards)
    assert result["ok"] is False
    assert "rollback stopped at c2" in result["error"]


@pytest.mark.parametrize(
    "cards",
    [
        (),
        ({"title": "a", "parents": (1,)}, {"title": "b"}),
        ({"title": "a", "parents": (0,)},),
        ({"title": "a", "kind": "review"},),
        ({"title": "a", "kind": "other", "gate": True},),
        ({"title": " "},),
    ],
)
def test_a_bad_card_list_creates_nothing(tmp_path, cards):
    board = Board()
    assert _create(tmp_path, board, cards=cards)["ok"] is False
    assert board.cards == {}


def test_same_reject_id_counts_once(tmp_path):
    config, state, _, _ = _ready(tmp_path)
    board = Board()
    board.cards["g1"] = _gate()
    first = _reject(config, state, board)
    again = _reject(config, state, board)
    assert first["round"] == 1
    assert again == first
    assert [call[0] for call in board.calls] == ["worker"]


def test_the_fourth_reject_leaves_the_card_in_review_and_opens_nothing(tmp_path):
    config, state, _, _ = _ready(tmp_path)
    board = Board()
    board.cards["g1"] = _gate()
    for index in range(1, 4):
        at = NOW + timedelta(minutes=index)
        opened = _reject(config, state, board, f"r{index}", at=at)
        assert opened["round"] == index
        assert board.cards["g1"]["status"] == "ready"
        board.cards["g1"]["status"] = "review"
    capped = _reject(config, state, board, "r4", at=NOW + timedelta(minutes=4))
    assert capped["error"] == "reject cap"
    assert capped["opened"] is False
    assert board.cards["g1"]["status"] == "review"
    assert len(board.calls) == 3


def test_a_reject_whose_record_failed_finishes_once_even_after_a_new_review(tmp_path):
    config, state, _, _ = _ready(tmp_path)
    board = Board()
    board.cards["g1"] = _gate()
    real = state.append_record

    def fail_done(name, payload, now):
        if payload.get("phase") == "done":
            raise OSError("disk full")
        real(name, payload, now)

    state.append_record = fail_done
    with pytest.raises(OSError):
        _reject(config, state, board)
    assert board.cards["g1"]["status"] == "ready"
    # The worker ran again and asked for review again before the retry arrives.
    board.cards["g1"]["status"] = "review"
    state.append_record = real
    retried = _reject(config, state, board, at=NOW + timedelta(minutes=5))
    assert retried["ok"] is True
    assert retried["round"] == 1
    assert [call[0] for call in board.calls] == ["worker"]
    assert board.cards["g1"]["status"] == "review"
    assert _reject(config, state, board) == retried


def test_a_reject_that_failed_on_the_board_is_retried_with_the_same_id(tmp_path):
    config, state, _, _ = _ready(tmp_path)
    board = Board()
    board.cards["g1"] = _gate()
    real = board.reject_worker

    def broken(card_id, text):
        raise OSError("busy")

    board.reject_worker = broken
    failed = _reject(config, state, board)
    assert failed["ok"] is False
    assert failed["retry"] is True
    board.reject_worker = real
    done = _reject(config, state, board)
    assert done["ok"] is True
    assert done["round"] == 1
    assert len(board.calls) == 1


def test_a_reject_id_cannot_move_to_another_card(tmp_path):
    config, state, _, _ = _ready(tmp_path)
    board = Board()
    board.cards["g1"] = _gate()
    board.cards["g2"] = _gate()
    _reject(config, state, board)
    moved = _reject(config, state, board, card_id="g2")
    assert moved["ok"] is False
    assert [call[1] for call in board.calls] == ["g1"]


def test_a_reject_in_the_same_second_as_the_earlier_reopen_still_reopens(tmp_path):
    config, state, _, _ = _ready(tmp_path)
    board = Board()
    board.cards["g1"] = _gate()
    assert _reject(config, state, board, "r1")["round"] == 1
    board.cards["g1"]["status"] = "review"
    assert _reject(config, state, board, "r2")["round"] == 2
    # r1's reopen event is not r2's: the card really went back twice.
    assert [call[0] for call in board.calls] == ["worker", "worker"]
    assert board.cards["g1"]["status"] == "ready"


def test_a_reject_that_failed_does_not_use_a_round_and_blocks_a_new_id(tmp_path):
    config, state, _, _ = _ready(tmp_path)
    board = Board()
    board.cards["g1"] = _gate()
    real = board.reject_worker

    def broken(card_id, text):
        raise OSError("busy")

    board.reject_worker = broken
    assert _reject(config, state, board, "r1")["retry"] is True
    refused = _reject(config, state, board, "r2")
    assert refused["ok"] is False
    assert refused["pending"] == "r1"
    assert board.calls == []
    board.reject_worker = real
    assert _reject(config, state, board, "r1")["round"] == 1
    board.cards["g1"]["status"] = "review"
    assert _reject(config, state, board, "r2")["round"] == 2


def test_a_pending_reject_on_one_card_does_not_block_another_card(tmp_path):
    config, state, _, _ = _ready(tmp_path)
    board = Board()
    board.cards["g1"] = _gate()
    board.cards["g2"] = _gate()
    real = board.reject_worker

    def broken(card_id, text):
        raise OSError("busy")

    board.reject_worker = broken
    assert _reject(config, state, board, "ra", card_id="g1")["retry"] is True
    board.reject_worker = real
    opened = _reject(config, state, board, "rb", card_id="g2")
    assert opened["ok"] is True
    assert opened["round"] == 1
    assert [call[1] for call in board.calls] == ["g2"]


def test_a_late_retry_of_a_pending_reject_never_passes_the_cap(tmp_path):
    config, state, _, _ = _ready(tmp_path)
    board = Board()
    board.cards["g1"] = _gate()
    for index in (1, 2):
        assert _reject(config, state, board, f"r{index}")["round"] == index
        board.cards["g1"]["status"] = "review"
    real = board.reject_worker

    def broken(card_id, text):
        raise OSError("busy")

    board.reject_worker = broken
    assert _reject(config, state, board, "r3")["retry"] is True
    assert _reject(config, state, board, "r4")["pending"] == "r3"
    board.reject_worker = real
    assert _reject(config, state, board, "r3")["round"] == 3
    board.cards["g1"]["status"] = "review"
    assert _reject(config, state, board, "r4")["error"] == "reject cap"
    assert len(board.calls) == 3


def test_two_callers_rejecting_one_id_reopen_the_card_once(tmp_path):
    config, state, _, _ = _ready(tmp_path)
    board = Board()
    board.reopen_delay = 0.2
    board.cards["g1"] = _gate()
    # Each caller has its own State, as two processes would. Only the file lock joins them.
    states = [State(state.root) for _ in range(2)]
    results = in_threads([lambda s=s: _reject(config, s, board, "r1") for s in states])
    assert results[0] == results[1]
    assert results[0]["round"] == 1
    assert len(board.calls) == 1
    records = state.read_records("rejects.jsonl")
    assert [item["phase"] for item in records] == ["started", "done"]


def test_two_callers_rejecting_with_different_ids_open_one_round(tmp_path):
    config, state, _, _ = _ready(tmp_path)
    board = Board()
    board.reopen_delay = 0.2
    board.cards["g1"] = _gate()
    states = [State(state.root) for _ in range(2)]
    jobs = [lambda s=s, i=i: _reject(config, s, board, f"r{i}") for i, s in enumerate(states)]
    results = in_threads(jobs)
    assert sorted(item["ok"] for item in results) == [False, True]
    assert len(board.calls) == 1


def test_review_gate_reject_is_refused_and_does_not_mutate(tmp_path):
    config, state, _, _ = _ready(tmp_path)
    board = Board()
    board.cards["g1"] = _gate("review")
    result = _reject(config, state, board)
    assert result["error"] == "core update needed"
    assert board.calls == []
    assert board.cards["g1"]["status"] == "review"
    assert state.read_records("rejects.jsonl") == ()


@pytest.mark.parametrize("phase", ["done", "started"])
def test_review_gate_reject_refuses_historical_records_without_mutation(tmp_path, phase):
    config, state, _, _ = _ready(tmp_path)
    board = Board()
    board.cards["g1"] = _gate("review")
    state.append_record(
        "rejects.jsonl",
        {
            "card_id": "g1",
            "reject_id": "historical",
            "phase": phase,
            "round": 1,
            "event_mark": 0,
            "result": {"ok": True, "opened": True, "round": 1, "reject_id": "historical"},
        },
        NOW,
    )
    before = state.read_records("rejects.jsonl")
    card_before = dict(board.cards["g1"])
    for reject_id in ("historical", "new"):
        result = _reject(config, state, board, reject_id)
        assert result == {"ok": False, "error": "core update needed", "opened": False}
    assert board.cards["g1"] == card_before
    assert board.calls == []
    assert board.reopened == []
    assert state.read_records("rejects.jsonl") == before


def test_a_card_that_is_not_a_gate_or_not_waiting_is_not_rejected(tmp_path):
    config, state, _, _ = _ready(tmp_path)
    board = Board()
    board.cards["g1"] = _gate(status="running")
    board.cards["g2"] = _gate("unknown")
    assert _reject(config, state, board)["error"] == "card is not waiting for a decision"
    assert _reject(config, state, board, "r2", "g2")["error"] == "unknown gate role"
    assert board.calls == []


def test_create_opens_the_live_board_when_none_is_injected(tmp_path, monkeypatch):
    from taskwatch.tools import taskwatch_create

    config, _, intent, spec = _ready(tmp_path)
    seen = {}

    def fake(slug=None, db_path=None):
        del db_path
        seen["slug"] = slug
        return Board()

    monkeypatch.setattr("taskwatch.tools.hermes_root", lambda: tmp_path / "hermes")
    monkeypatch.setattr("taskwatch.hermes_board.open_board", fake)
    result = json.loads(
        taskwatch_create(
            {
                "approval_key": "apr-1",
                "intent": str(intent),
                "spec": str(spec),
                "cards": [{"title": "work", "gate": True}],
            }
        )
    )
    assert seen["slug"] == config.board
    assert result["ok"] is True
