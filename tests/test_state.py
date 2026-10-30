import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from taskwatch.state import RECORD_VERSION, RecordError, State, state_dir

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 26, 0, 5, tzinfo=timezone.utc)
NAME = "records.jsonl"


def _add(state: State, n: int, now: datetime = NOW) -> None:
    state.append_record(NAME, {"n": n}, now)


def _numbers(state: State) -> list[int]:
    return [row["n"] for row in state.read_records(NAME)]


def _path(state: State) -> Path:
    return state.dir / NAME


def test_state_dir_is_under_the_hermes_root(tmp_path):
    assert state_dir(tmp_path) == tmp_path / "taskwatch" / "state"


def test_records_are_versioned_and_append_only(tmp_path):
    state = State(tmp_path)
    _add(state, 1)
    _add(state, 2, LATER)
    assert state.read_records(NAME) == (
        {"version": RECORD_VERSION, "n": 1},
        {"version": RECORD_VERSION, "n": 2},
    )
    assert _path(state).read_text(encoding="utf-8").endswith("\n")


def test_missing_file_reads_as_no_records(tmp_path):
    assert State(tmp_path).read_records(NAME) == ()


def test_torn_tail_is_preserved_and_the_next_write_reads(tmp_path):
    state = State(tmp_path)
    _add(state, 1)
    with _path(state).open("ab") as handle:
        handle.write(b'{"kind":"torn"')
    assert _numbers(state) == [1]

    _add(state, 2, LATER)
    torn = list(state.dir.glob(f"{NAME}.torn-*"))
    assert len(torn) == 1
    assert torn[0].read_bytes() == b'{"kind":"torn"'
    assert _path(state).read_bytes().endswith(b"\n")
    assert _numbers(state) == [1, 2]


def test_middle_line_is_unreadable(tmp_path):
    state = State(tmp_path)
    _add(state, 1)
    good = _path(state).read_text(encoding="utf-8")
    _path(state).write_text(good + "{not json}\n", encoding="utf-8")
    with pytest.raises(RecordError, match="not json"):
        state.read_records(NAME)


def test_unknown_version_is_unreadable(tmp_path):
    state = State(tmp_path)
    _add(state, 1)
    line = _path(state).read_text(encoding="utf-8")
    _path(state).write_text(line.replace('"version":1', '"version":9', 1), encoding="utf-8")
    with pytest.raises(RecordError, match="unknown version"):
        state.read_records(NAME)


def test_unreadable_file_is_an_error_not_empty_success(tmp_path, monkeypatch):
    state = State(tmp_path)
    state.dir.mkdir(parents=True)
    _path(state).write_text("x", encoding="utf-8")

    def _boom(self):
        raise OSError("denied")

    monkeypatch.setattr(type(_path(state)), "read_bytes", _boom)
    with pytest.raises(RecordError, match="unreadable"):
        state.read_records(NAME)


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
        _add(state, 1)
        finished.set()

    writer = threading.Thread(target=_write)
    writer.start()
    assert finished.wait(0.2) is False
    release.set()
    writer.join(2)
    holder.join(2)
    assert finished.is_set()
    assert _numbers(state) == [1]


def test_nested_lock_reuses_the_same_owner(tmp_path):
    state = State(tmp_path)
    done = threading.Event()

    def _nested():
        with state.lock():
            _add(state, 1)
            assert _numbers(state) == [1]
            _add(state, 2)
        done.set()

    thread = threading.Thread(target=_nested, daemon=True)
    thread.start()
    assert done.wait(2)
    thread.join(1)
    assert _numbers(state) == [1, 2]


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
    _add(state, 1)
    good = _path(state).read_bytes()
    _path(state).write_bytes(good + b"torn")
    real_write = Path.write_bytes

    def write_bytes(self, data):
        if self == _path(state):
            os.truncate(self, 0)
            raise OSError("rewrite refused")
        return real_write(self, data)

    def ftruncate(fd, length):
        raise OSError("truncate failed")

    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    monkeypatch.setattr(os, "ftruncate", ftruncate)
    with pytest.raises(OSError):
        _add(state, 2, LATER)
    assert _path(state).read_bytes().startswith(good)
    torn = list(state.dir.glob(f"{NAME}.torn-*"))
    assert len(torn) == 1
    assert torn[0].read_bytes() == b"torn"


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
    _add(state, 1)
    line = _path(state).read_bytes()
    _fail_fsync_of(monkeypatch, _path(state), "line fsync failed")
    with pytest.raises(OSError, match="line fsync failed"):
        _add(state, 2, LATER)
    monkeypatch.undo()
    assert _numbers(state) == [1]
    assert _path(state).read_bytes() == line
    _add(state, 2, LATER)
    assert _numbers(state) == [1, 2]


def test_fsync_failure_is_not_success_and_keeps_confirmed_records(tmp_path, monkeypatch):
    state = State(tmp_path)
    _add(state, 1)

    def flaky(_fd):
        raise OSError("fsync failed")

    monkeypatch.setattr(os, "fsync", flaky)
    with pytest.raises(OSError):
        _add(state, 2, LATER)
    monkeypatch.undo()
    assert _numbers(state) == [1]


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
                    _add(state, 1)
                except Exception:
                    os._exit(1)
                os._exit(0)
            deadline = time.monotonic() + 5
            while not trying.exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            time.sleep(0.3)
            assert not _path(state).exists()
        _pid, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        finished = True
        assert _numbers(state) == [1]
    finally:
        if pid and not finished:
            os.kill(pid, 9)
            os.waitpid(pid, 0)


def _torn_prefix(state: State) -> bytes:
    _add(state, 1)
    good = _path(state).read_bytes()
    _path(state).write_bytes(good + b"torn")
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
        _add(state, 2, LATER)
    assert _path(state).read_bytes() == good + b"torn"
    monkeypatch.undo()
    _add(state, 2, LATER)
    assert _numbers(state) == [1, 2]


def test_torn_truncate_fsync_is_required(tmp_path, monkeypatch):
    state = State(tmp_path)
    good = _torn_prefix(state)
    target = _path(state).resolve()
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
        _add(state, 2, LATER)
    assert _path(state).read_bytes() == good
    torn = list(state.dir.glob(f"{NAME}.torn-*"))
    assert len(torn) == 1
    assert torn[0].read_bytes() == b"torn"
    monkeypatch.undo()
    _add(state, 2, LATER)
    assert _numbers(state) == [1, 2]


def test_torn_side_file_name_is_durable_before_the_cut(tmp_path, monkeypatch):
    state = State(tmp_path)
    good = _torn_prefix(state)
    _fail_fsync_of(monkeypatch, state.dir, "side name fsync failed")
    with pytest.raises(OSError, match="side name fsync failed"):
        _add(state, 2, LATER)
    assert _path(state).read_bytes() == good + b"torn"
    monkeypatch.undo()
    _add(state, 2, LATER)
    assert _numbers(state) == [1, 2]


def test_failed_append_syncs_its_cut(tmp_path, monkeypatch):
    state = State(tmp_path)
    _add(state, 1)
    target = _path(state).resolve()
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
        _add(state, 2, LATER)
    assert events == ["sync", "cut", "sync"]


@pytest.mark.parametrize("place", ["", "taskwatch"], ids=["taskwatch-dir-name", "state-dir-name"])
def test_new_state_directory_names_are_durable(tmp_path, monkeypatch, place):
    state = State(tmp_path)
    _fail_fsync_of(monkeypatch, tmp_path / place, "dir name fsync failed")
    with pytest.raises(OSError, match="dir name fsync failed"):
        _add(state, 1)
    assert not _path(state).exists()


def test_new_jsonl_name_is_durable(tmp_path, monkeypatch):
    state = State(tmp_path)
    _fail_fsync_of(monkeypatch, state.dir, "name fsync failed")
    with pytest.raises(OSError, match="name fsync failed"):
        _add(state, 1)
    monkeypatch.undo()
    assert state.read_records(NAME) == ()
    _add(state, 1)
    assert _numbers(state) == [1]


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


@pytest.mark.parametrize("where", ["root", "taskwatch"])
def test_retry_after_failed_parent_fsync_syncs_that_parent_before_succeeding(
    tmp_path, monkeypatch, where
):
    state = State(tmp_path)
    places = {"root": tmp_path, "taskwatch": tmp_path / "taskwatch"}
    done = _record_fsyncs(monkeypatch, places[where])
    with pytest.raises(OSError, match="parent fsync failed"):
        _add(state, 1)
    done.clear()
    _add(state, 1)
    assert places[where].resolve() in done
    assert _numbers(state) == [1]
