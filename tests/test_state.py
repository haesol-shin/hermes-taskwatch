import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from taskwatch.state import (
    RECORD_VERSION,
    RecordError,
    Review,
    State,
    StateError,
    Warning,
    state_dir,
)

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 26, 0, 5, tzinfo=timezone.utc)


def _review(card: str = "card-1", round_n: int = 1) -> Review:
    return Review(
        version=RECORD_VERSION,
        round=round_n,
        card=card,
        fingerprint="abc",
        basis="basis-1",
        reviewer="reviewer.invalid",
        verdict="PASS",
        blocking=False,
    )


def test_state_dir_is_under_the_hermes_root(tmp_path):
    assert state_dir(tmp_path) == tmp_path / "taskwatch" / "state"


def test_start_is_append_only_and_has_no_fingerprint(tmp_path):
    state = State(tmp_path)
    state.append_start("apr-1", NOW, "start the work")
    loaded = state.load()

    assert loaded.error is None
    assert loaded.approvals[0].kind == "start"
    assert loaded.approvals[0].card_id is None
    assert loaded.approvals[0].fingerprint is None
    assert loaded.approvals[0].approved_at == NOW
    text = state.approvals_path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert "fingerprint" not in text


def test_duplicate_start_and_conflicting_link_do_not_rewrite(tmp_path):
    state = State(tmp_path)
    state.append_start("apr-1", NOW, "start the work")
    before = state.approvals_path.read_bytes()
    with pytest.raises(StateError, match="duplicate start"):
        state.append_start("apr-1", LATER, "again")
    assert state.approvals_path.read_bytes() == before

    state.append_link("apr-1", "card-1", LATER)
    linked = state.approvals_path.read_bytes()
    state.append_link("apr-1", "card-1", LATER)
    assert state.approvals_path.read_bytes() == linked
    with pytest.raises(StateError, match="conflicting link"):
        state.append_link("apr-1", "card-2", LATER)
    assert state.approvals_path.read_bytes() == linked
    assert [item.card_id for item in state.load().approvals if item.kind == "link"] == ["card-1"]


def test_link_requires_a_start(tmp_path):
    state = State(tmp_path)
    with pytest.raises(StateError, match="link without start"):
        state.append_link("apr-1", "card-1", NOW)


def test_apply_keeps_the_fingerprint_on_its_own_line(tmp_path):
    state = State(tmp_path)
    state.append_apply("merge", "msg-1", "card-1", "sha-1", NOW)
    apply = state.load().approvals[0]
    assert apply.kind == "apply"
    assert apply.fingerprint == "sha-1"
    assert apply.message_id == "msg-1"


def test_torn_tail_is_preserved_and_the_next_write_reads(tmp_path):
    state = State(tmp_path)
    state.append_start("apr-1", NOW, "start the work")
    with state.approvals_path.open("ab") as handle:
        handle.write(b'{"kind":"torn"')
    loaded = state.load()
    assert loaded.error is None
    assert len(loaded.approvals) == 1

    state.append_link("apr-1", "card-1", LATER)
    torn = list(state.dir.glob("approvals.jsonl.torn-*"))
    assert len(torn) == 1
    assert torn[0].read_bytes() == b'{"kind":"torn"'
    assert state.approvals_path.read_bytes().endswith(b"\n")
    assert [item.kind for item in state.load().approvals] == ["start", "link"]


def test_middle_line_is_unreadable(tmp_path):
    state = State(tmp_path)
    state.append_start("apr-1", NOW, "start the work")
    good = state.approvals_path.read_text(encoding="utf-8")
    state.approvals_path.write_text(good + "{not json}\n", encoding="utf-8")
    loaded = state.load()
    assert loaded.error is not None
    assert loaded.approvals == ()


def test_unknown_version_is_unreadable(tmp_path):
    from taskwatch.state import _read_approvals

    state = State(tmp_path)
    state.append_start("apr-1", NOW, "start the work")
    line = state.approvals_path.read_text(encoding="utf-8")
    state.approvals_path.write_text(line.replace('"version":1', '"version":9', 1), encoding="utf-8")
    with pytest.raises(RecordError, match="unknown version"):
        _read_approvals(state.approvals_path)
    assert state.load().error is not None
    assert state.load().approvals == ()


def test_review_body_is_not_stored_in_the_json_line(tmp_path):
    state = State(tmp_path)
    marker = "BODY-MARKER-not-in-jsonl"
    path = state.append_review(_review(), marker + "\n", NOW)
    assert marker in path.read_text(encoding="utf-8")
    assert marker not in state.reviews_path.read_text(encoding="utf-8")
    review = state.load().reviews[0]
    assert review.blocking is False
    assert review.basis == "basis-1"


def test_warning_history_round_trip(tmp_path):
    state = State(tmp_path)
    state.append_warning(Warning(RECORD_VERSION, "card-1:ev-1", NOW, "internal", "no action"))
    warning = state.load().warnings[0]
    assert warning.channel == "internal"
    assert warning.warned_at == NOW


def test_naive_time_is_refused(tmp_path):
    state = State(tmp_path)
    with pytest.raises(StateError, match="timezone-aware"):
        state.append_start("apr-1", datetime(2026, 10, 26), "start the work")


def test_unreadable_file_is_an_error_not_empty_success(tmp_path, monkeypatch):
    state = State(tmp_path)
    state.dir.mkdir(parents=True)
    state.approvals_path.write_text("x", encoding="utf-8")

    def _boom(self):
        raise OSError("denied")

    monkeypatch.setattr(type(state.approvals_path), "read_bytes", _boom)
    assert state.load().error is not None


def test_one_lock_blocks_a_second_writer(tmp_path):
    state = State(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def _hold():
        with state.lock():
            started.set()
            assert release.wait(2)

    holder = threading.Thread(target=_hold)
    holder.start()
    assert started.wait(1)
    finished = threading.Event()

    def _write():
        state.append_start("apr-1", NOW, "start the work")
        finished.set()

    writer = threading.Thread(target=_write)
    writer.start()
    assert finished.wait(0.2) is False
    release.set()
    writer.join(2)
    holder.join(2)
    assert finished.is_set()
    assert state.load().approvals[0].approval_key == "apr-1"


def test_nested_lock_reuses_the_same_owner(tmp_path):
    state = State(tmp_path)
    done = threading.Event()

    def _nested():
        with state.lock():
            state.append_start("apr-1", NOW, "start the work")
            assert state.load().error is None
            state.append_link("apr-1", "card-1", NOW)
        done.set()

    thread = threading.Thread(target=_nested, daemon=True)
    thread.start()
    assert done.wait(2)
    thread.join(1)
    assert any(item.kind == "link" for item in state.load().approvals)


def test_lock_excludes_another_process(tmp_path):
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from taskwatch.state import State\n"
        "root, ready, release = map(Path, sys.argv[1:])\n"
        "with State(root).lock():\n"
        "    ready.write_text('held')\n"
        "    while not release.exists():\n"
        "        pass\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(ready), str(release)])
    acquired = threading.Event()

    def _take():
        with State(tmp_path).lock():
            acquired.set()

    waiter = threading.Thread(target=_take, daemon=True)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists():
            assert proc.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.05)
        waiter.start()
        assert acquired.wait(0.3) is False
    finally:
        release.write_text("go")
        proc.wait(5)
    waiter.join(2)
    assert acquired.is_set()


def test_torn_repair_keeps_the_prefix_if_a_later_step_fails(tmp_path, monkeypatch):
    state = State(tmp_path)
    state.append_start("apr-1", NOW, "start the work")
    good = state.approvals_path.read_bytes()
    state.approvals_path.write_bytes(good + b"torn")
    real_write = Path.write_bytes

    def write_bytes(self, data):
        if self == state.approvals_path:
            os.truncate(self, 0)
            raise OSError("rewrite refused")
        return real_write(self, data)

    def ftruncate(fd, length):
        raise OSError("truncate failed")

    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    monkeypatch.setattr(os, "ftruncate", ftruncate)
    with pytest.raises(OSError):
        state.append_link("apr-1", "card-1", LATER)
    assert state.approvals_path.read_bytes().startswith(good)
    torn = list(state.dir.glob("approvals.jsonl.torn-*"))
    assert len(torn) == 1
    assert torn[0].read_bytes() == b"torn"


def test_duplicate_review_is_refused_before_any_write(tmp_path):
    state = State(tmp_path)
    body = state.append_review(_review(), "original body\n", NOW)
    line = state.reviews_path.read_bytes()
    bodies = sorted(state.bodies_dir.iterdir())
    with pytest.raises(StateError, match="duplicate review"):
        state.append_review(_review(), "replacement body\n", LATER)
    assert sorted(state.bodies_dir.iterdir()) == bodies
    assert body.read_text(encoding="utf-8") == "original body\n"
    assert state.reviews_path.read_bytes() == line


def test_each_review_gets_its_own_body_file(tmp_path):
    state = State(tmp_path)
    first = state.append_review(_review(round_n=1), "first\n", NOW)
    second = state.append_review(_review(round_n=2), "second\n", LATER)
    assert first != second
    assert first.read_text(encoding="utf-8") == "first\n"
    assert second.read_text(encoding="utf-8") == "second\n"
    assert [item.body_file for item in state.load().reviews] == [first.name, second.name]


def _fd_path(fd: int) -> Path:
    return Path(os.readlink(f"/proc/self/fd/{fd}"))


def _fail_fsync_of(monkeypatch, path: Path, message: str = "fsync failed", nth: int = 1) -> None:
    """Fail the nth fsync of exactly this file or directory, and nothing else."""
    target = path.resolve()
    real = os.fsync
    seen = {"n": 0}

    def boom(fd):
        if _fd_path(fd) == target:
            seen["n"] += 1
            if seen["n"] == nth:
                raise OSError(message)
        return real(fd)

    monkeypatch.setattr(os, "fsync", boom)


def test_line_fsync_failure_leaves_no_consumable_record_and_retry_succeeds(tmp_path, monkeypatch):
    state = State(tmp_path)
    earlier = state.append_review(_review(), "earlier\n", NOW)
    line = state.reviews_path.read_bytes()
    _fail_fsync_of(monkeypatch, state.reviews_path, "line fsync failed")
    with pytest.raises(OSError, match="line fsync failed"):
        state.append_review(_review("card-2"), "lost\n", LATER)
    monkeypatch.undo()
    loaded = state.load()
    assert loaded.error is None
    assert [item.card for item in loaded.reviews] == ["card-1"]
    assert state.reviews_path.read_bytes() == line
    assert earlier.read_text(encoding="utf-8") == "earlier\n"
    retry = state.append_review(_review("card-2"), "retry\n", LATER)
    assert retry.read_text(encoding="utf-8") == "retry\n"
    assert [item.card for item in state.load().reviews] == ["card-1", "card-2"]


def test_crash_between_body_and_line_leaves_the_body_unused(tmp_path):
    import taskwatch.state as module

    state = State(tmp_path)
    pid = os.fork()
    if pid == 0:
        try:
            module._append_line = lambda *_args, **_kwargs: os._exit(9)
            state.append_review(_review(), "crashed\n", NOW)
        finally:
            os._exit(0)
    _pid, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 9
    (orphan,) = state.bodies_dir.iterdir()
    assert state.load().reviews == ()
    retry = state.append_review(_review(), "retry\n", LATER)
    assert orphan.read_text(encoding="utf-8") == "crashed\n"
    assert retry != orphan
    (review,) = state.load().reviews
    assert review.body_file == retry.name


def test_record_whose_body_is_gone_or_changed_is_not_loadable(tmp_path):
    state = State(tmp_path)
    body = state.append_review(_review(), "approved text\n", NOW)
    body.write_text("rewritten\n", encoding="utf-8")
    assert "hash" in (state.load().error or "")
    body.unlink()
    loaded = state.load()
    assert loaded.error
    assert loaded.reviews == ()


def test_writer_rejects_values_the_reader_cannot_load(tmp_path):
    state = State(tmp_path)
    bad_round = Review(RECORD_VERSION, 1.5, "card-1", "abc", "basis", "rev", "PASS", False)
    with pytest.raises(StateError, match="bad round"):
        state.append_review(bad_round, "body\n", NOW)
    bad_version = Review(True, 1, "card-1", "abc", "basis", "rev", "PASS", False)
    with pytest.raises(StateError, match="version"):
        state.append_review(bad_version, "body\n", NOW)
    with pytest.raises(StateError, match="version"):
        state.append_warning(Warning(True, "cause-1", NOW, "internal", "summary"))
    assert not state.reviews_path.exists()
    assert not state.warnings_path.exists()
    assert state.load().error is None


def test_non_string_enum_is_a_record_error(tmp_path):
    state = State(tmp_path)
    state.append_review(_review(), "body\n", NOW)
    text = state.reviews_path.read_text(encoding="utf-8")
    state.reviews_path.write_text(
        text.replace('"verdict":"PASS"', '"verdict":[]'),
        encoding="utf-8",
    )
    loaded = state.load()
    assert loaded.error
    assert loaded.reviews == ()

    other = State(tmp_path / "other")
    other.append_warning(Warning(RECORD_VERSION, "cause-1", NOW, "internal", "noted"))
    warn = other.warnings_path.read_text(encoding="utf-8")
    other.warnings_path.write_text(
        warn.replace('"channel":"internal"', '"channel":[]'),
        encoding="utf-8",
    )
    loaded = other.load()
    assert loaded.error
    assert loaded.warnings == ()


def test_lock_setup_failure_is_a_load_error(tmp_path):
    state = State(tmp_path)
    state.dir.parent.mkdir(parents=True)
    state.dir.write_text("not a directory", encoding="utf-8")
    loaded = state.load()
    assert loaded.error
    assert loaded.approvals == ()


def test_fsync_failure_is_not_success_and_keeps_confirmed_records(tmp_path, monkeypatch):
    state = State(tmp_path)
    kept = state.append_review(_review(), "kept\n", NOW)
    calls = {"n": 0}

    def flaky(_fd):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("fsync failed")

    monkeypatch.setattr(os, "fsync", flaky)
    with pytest.raises(OSError):
        state.append_review(_review("card-2"), "new\n", LATER)
    monkeypatch.undo()
    loaded = state.load()
    assert loaded.error is None
    assert any(item.card == "card-1" for item in loaded.reviews)
    assert kept.read_text(encoding="utf-8") == "kept\n"
    assert calls["n"] >= 2


def test_body_write_failure_leaves_no_consumable_review(tmp_path, monkeypatch):
    state = State(tmp_path)
    state.append_review(_review(), "kept\n", NOW)
    real = os.write

    def boom(fd, data):
        if "card-2" in os.path.basename(os.readlink(f"/proc/self/fd/{fd}")):
            raise OSError("body failed")
        return real(fd, data)

    monkeypatch.setattr(os, "write", boom)
    with pytest.raises(OSError, match="body failed"):
        state.append_review(_review("card-2"), "lost\n", LATER)
    loaded = state.load()
    assert loaded.error is None
    assert [item.card for item in loaded.reviews] == ["card-1"]
    monkeypatch.undo()
    path = state.append_review(_review("card-2"), "retry\n", LATER)
    assert path.read_text(encoding="utf-8") == "retry\n"
    assert [item.card for item in state.load().reviews] == ["card-1", "card-2"]


def test_body_fsync_failure_leaves_no_consumable_review(tmp_path, monkeypatch):
    state = State(tmp_path)
    kept = state.append_review(_review(), "kept\n", NOW)
    real = os.fsync

    def boom(fd):
        name = os.path.basename(os.readlink(f"/proc/self/fd/{fd}"))
        if "card-2" in name:
            raise OSError("body fsync failed")
        return real(fd)

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError, match="body fsync failed"):
        state.append_review(_review("card-2"), "lost\n", LATER)
    loaded = state.load()
    assert loaded.error is None
    assert [item.card for item in loaded.reviews] == ["card-1"]
    assert kept.read_text(encoding="utf-8") == "kept\n"


def test_forked_child_cannot_reuse_the_parent_lock(tmp_path):
    state = State(tmp_path)
    trying = tmp_path / "trying"
    pid = 0
    finished = False
    try:
        with state.lock():
            # The child inherits the parent's lock bookkeeping, not the lock.
            pid = os.fork()
            if pid == 0:
                try:
                    trying.write_text("1", encoding="utf-8")
                    state.append_start("apr-1", NOW, "from child")
                except Exception:
                    os._exit(1)
                os._exit(0)
            deadline = time.monotonic() + 5
            while not trying.exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            time.sleep(0.3)
            assert not state.approvals_path.exists()
        _pid, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        finished = True
        assert state.load().approvals[0].approval_key == "apr-1"
    finally:
        if pid and not finished:
            os.kill(pid, 9)
            os.waitpid(pid, 0)


def _torn_prefix(state: State) -> bytes:
    state.append_start("apr-1", NOW, "start the work")
    good = state.approvals_path.read_bytes()
    state.approvals_path.write_bytes(good + b"torn")
    return good


def test_torn_side_file_fsync_is_required(tmp_path, monkeypatch):
    state = State(tmp_path)
    good = _torn_prefix(state)
    real = os.fsync

    def boom(fd):
        if ".torn-" in os.path.basename(os.readlink(f"/proc/self/fd/{fd}")):
            raise OSError("side fsync failed")
        return real(fd)

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError, match="side fsync failed"):
        state.append_link("apr-1", "card-1", LATER)
    assert state.approvals_path.read_bytes() == good + b"torn"
    monkeypatch.undo()
    state.append_link("apr-1", "card-1", LATER)
    assert [item.kind for item in state.load().approvals] == ["start", "link"]


def test_torn_truncate_fsync_is_required(tmp_path, monkeypatch):
    state = State(tmp_path)
    good = _torn_prefix(state)
    target = state.approvals_path.resolve()
    real_fsync, real_truncate, real_write = os.fsync, os.ftruncate, os.write
    just_cut = {"yes": False}

    def ftruncate(fd, length):
        real_truncate(fd, length)
        just_cut["yes"] = True

    def write(fd, data):
        just_cut["yes"] = False
        return real_write(fd, data)

    def fsync(fd):
        # Only the sync that directly follows the cut fails. The sync of the
        # appended line comes after a write and is not touched.
        if just_cut["yes"] and _fd_path(fd) == target:
            raise OSError("truncate fsync failed")
        return real_fsync(fd)

    monkeypatch.setattr(os, "ftruncate", ftruncate)
    monkeypatch.setattr(os, "write", write)
    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(OSError, match="truncate fsync failed"):
        state.append_link("apr-1", "card-1", LATER)
    assert state.approvals_path.read_bytes() == good
    torn = list(state.dir.glob("approvals.jsonl.torn-*"))
    assert len(torn) == 1
    assert torn[0].read_bytes() == b"torn"
    monkeypatch.undo()
    state.append_link("apr-1", "card-1", LATER)
    assert [item.kind for item in state.load().approvals] == ["start", "link"]


def test_torn_side_file_name_is_durable_before_the_cut(tmp_path, monkeypatch):
    state = State(tmp_path)
    good = _torn_prefix(state)
    _fail_fsync_of(monkeypatch, state.dir, "side name fsync failed")
    with pytest.raises(OSError, match="side name fsync failed"):
        state.append_link("apr-1", "card-1", LATER)
    assert state.approvals_path.read_bytes() == good + b"torn"
    monkeypatch.undo()
    state.append_link("apr-1", "card-1", LATER)
    assert [item.kind for item in state.load().approvals] == ["start", "link"]


def test_failed_append_syncs_its_cut(tmp_path, monkeypatch):
    state = State(tmp_path)
    state.append_review(_review(), "earlier\n", NOW)
    target = state.reviews_path.resolve()
    real_fsync, real_truncate = os.fsync, os.ftruncate
    events: list[str] = []

    def ftruncate(fd, length):
        events.append("cut")
        return real_truncate(fd, length)

    def fsync(fd):
        if _fd_path(fd) == target:
            events.append("sync")
            if events.count("sync") == 1:
                raise OSError("line fsync failed")
        return real_fsync(fd)

    monkeypatch.setattr(os, "ftruncate", ftruncate)
    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(OSError, match="line fsync failed"):
        state.append_review(_review("card-2"), "lost\n", LATER)
    assert events == ["sync", "cut", "sync"]


@pytest.mark.parametrize("place", ["", "taskwatch"], ids=["taskwatch-dir-name", "state-dir-name"])
def test_new_state_directory_names_are_durable(tmp_path, monkeypatch, place):
    state = State(tmp_path)
    _fail_fsync_of(monkeypatch, tmp_path / place, "dir name fsync failed")
    with pytest.raises(OSError, match="dir name fsync failed"):
        state.append_start("apr-1", NOW, "start the work")
    assert not state.approvals_path.exists()


def test_new_jsonl_name_is_durable(tmp_path, monkeypatch):
    state = State(tmp_path)
    _fail_fsync_of(monkeypatch, state.dir, "name fsync failed")
    with pytest.raises(OSError, match="name fsync failed"):
        state.append_start("apr-1", NOW, "start the work")
    monkeypatch.undo()
    loaded = state.load()
    assert loaded.error is None
    assert loaded.approvals == ()
    state.append_start("apr-1", NOW, "start the work")
    assert [item.kind for item in state.load().approvals] == ["start"]


def _record_fsyncs(monkeypatch, fail: Path) -> list[Path]:
    """Fail the first fsync of `fail`; return the list that collects each successful fsync."""
    target = fail.resolve()
    real = os.fsync
    state = {"failed": False}
    done: list[Path] = []

    def fsync(fd):
        path = _fd_path(fd)
        if path == target and not state["failed"]:
            state["failed"] = True
            raise OSError("parent fsync failed")
        real(fd)
        done.append(path)

    monkeypatch.setattr(os, "fsync", fsync)
    return done


@pytest.mark.parametrize("where", ["root", "taskwatch", "reviews"])
def test_retry_after_failed_parent_fsync_syncs_that_parent_before_succeeding(
    tmp_path, monkeypatch, where
):
    state = State(tmp_path)
    places = {
        "root": tmp_path,
        "taskwatch": tmp_path / "taskwatch",
        "reviews": state.dir / "reviews",
    }
    if where == "reviews":
        state.append_start("apr-1", NOW, "start the work")
        call = lambda: state.append_review(_review(), "body\n", NOW)  # noqa: E731
    else:
        call = lambda: state.append_start("apr-1", NOW, "start the work")  # noqa: E731
    done = _record_fsyncs(monkeypatch, places[where])
    with pytest.raises(OSError, match="parent fsync failed"):
        call()
    done.clear()
    call()
    assert places[where].resolve() in done
    assert state.load().error is None


@pytest.mark.parametrize(
    ("where", "nth"),
    [("state", 1), ("reviews", 1), ("bodies", 1), ("state", 2)],
    ids=["reviews-dir-name", "bodies-dir-name", "body-file-name", "reviews-jsonl-name"],
)
def test_each_new_review_name_is_durable_before_it_is_used(tmp_path, monkeypatch, where, nth):
    state = State(tmp_path)
    state.append_start("apr-1", NOW, "start the work")
    places = {"state": state.dir, "reviews": state.dir / "reviews", "bodies": state.bodies_dir}
    _fail_fsync_of(monkeypatch, places[where], "name fsync failed", nth)
    with pytest.raises(OSError, match="name fsync failed"):
        state.append_review(_review(), "body\n", NOW)
    monkeypatch.undo()
    loaded = state.load()
    assert loaded.error is None
    assert loaded.reviews == ()
    state.append_review(_review(), "retry\n", LATER)
    assert [item.card for item in state.load().reviews] == ["card-1"]


@pytest.mark.parametrize("card", ["_card", "-card", ".card", ":card"])
def test_card_id_that_cannot_name_a_body_is_refused_before_any_write(tmp_path, card):
    state = State(tmp_path)
    state.append_review(_review(), "kept\n", NOW)
    line = state.reviews_path.read_bytes()
    bodies = sorted(state.bodies_dir.iterdir())
    with pytest.raises(StateError, match="bad id"):
        state.append_review(_review(card), "body\n", LATER)
    assert state.reviews_path.read_bytes() == line
    assert sorted(state.bodies_dir.iterdir()) == bodies
    loaded = state.load()
    assert loaded.error is None
    assert [item.card for item in loaded.reviews] == ["card-1"]


def test_every_card_id_the_writer_accepts_loads_back(tmp_path):
    state = State(tmp_path)
    cards = ["1", "a_b", "a:b", "A.b-c", "card-1"]
    for index, card in enumerate(cards):
        state.append_review(_review(card), f"body {index}\n", NOW)
    loaded = state.load()
    assert loaded.error is None
    assert [item.card for item in loaded.reviews] == cards
