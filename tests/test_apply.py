"""Apply copies or merges only the approved fingerprint."""

import dataclasses
import importlib.util
import os
import shutil
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from taskwatch.cli import handle_cli, setup_cli
from taskwatch.fingerprint import files_fingerprint
from taskwatch.state import RECORD_VERSION, Review, State
from tests.config_fixture import write_config
from tests.fake_board import FakeBoard
from tests.test_checks import _ready

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40


def _copy():
    spec = importlib.util.spec_from_file_location(
        "copy_snapshot", ROOT / "examples/apply/copy_snapshot.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.copy_snapshot


def _layout(tmp_path):
    root = tmp_path / "hermes"
    root.mkdir()
    write_config(root / "taskwatch" / "config.json")
    card = _ready(tmp_path, fingerprint=SHA, basis="basis-1", gate=True, status="review")
    state = State(root)
    review = Review(RECORD_VERSION, 1, card.id, SHA, "basis-1", "r.invalid", "PASS", False)
    state.append_review(review, "pass\n", NOW)
    state.append_apply("merge", "msg-1", card.id, SHA, NOW)
    return root, card


def test_apply_check_prints_the_fingerprint_and_fails_when_it_differs(tmp_path):
    import argparse

    root, card = _layout(tmp_path)
    parser = argparse.ArgumentParser()
    setup_cli(parser)
    text = handle_cli(
        parser.parse_args(["check", card.id, "--apply", "--root", str(root)]),
        {"board": FakeBoard((card,)), "now": NOW},
    )
    assert text == f"{SHA}\n"
    changed = dataclasses.replace(card, fingerprint="b" * 40)
    with pytest.raises(SystemExit) as caught:
        handle_cli(
            parser.parse_args(["check", card.id, "--apply", "--root", str(root)]),
            {"board": FakeBoard((changed,)), "now": NOW},
        )
    assert caught.value.code == 1


def test_copy_stops_when_the_hash_differs_and_copies_when_it_matches(tmp_path):
    source = tmp_path / "snap"
    source.mkdir()
    (source / "out.txt").write_text("ok\n", encoding="utf-8")
    approved = files_fingerprint(source, ("out.txt",))
    named = tmp_path / approved
    source.rename(named)
    dest = tmp_path / "dest"
    copy_snapshot = _copy()
    with pytest.raises(SystemExit):
        copy_snapshot(named, dest, "b" * 64)
    assert not dest.exists()
    copy_snapshot(named, dest, approved)
    assert (dest / "out.txt").read_text(encoding="utf-8") == "ok\n"


def test_copy_refuses_a_snapshot_that_changes_after_the_check(tmp_path, monkeypatch):
    source = tmp_path / "snap"
    source.mkdir()
    (source / "out.txt").write_text("APPROVED\n", encoding="utf-8")
    approved = files_fingerprint(source, ("out.txt",))
    named = tmp_path / approved
    source.rename(named)
    real = shutil.copytree

    def late_change(src, dst, *args, **kwargs):
        (Path(src) / "out.txt").write_text("UNAPPROVED\n", encoding="utf-8")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(shutil, "copytree", late_change)
    dest = tmp_path / "dest"
    with pytest.raises(SystemExit):
        _copy()(named, dest, approved)
    assert not dest.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == [approved]


def _exe(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_merge_script_does_not_call_gh_when_the_check_fails(tmp_path):
    hermes = tmp_path / "hermes-bin"
    gh = tmp_path / "gh"
    _exe(hermes, "#!/bin/sh\nexit 1\n")
    _exe(gh, '#!/bin/sh\nprintf \'%s\\n\' "$*" > "$GH_LOG"\n')
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["GH_LOG"] = str(tmp_path / "gh.log")
    completed = subprocess.run(
        [
            str(ROOT / "examples/apply/merge_pr.sh"),
            str(hermes),
            "/placeholder/root",
            "c1",
            "1",
            "squash",
        ],
        check=False,
        env=env,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert not (tmp_path / "gh.log").exists()


def test_merge_script_pins_the_checked_commit_and_requires_a_method(tmp_path):
    hermes = tmp_path / "hermes-bin"
    gh = tmp_path / "gh"
    _exe(hermes, f"#!/bin/sh\nprintf '%s\\n' {SHA}\n")
    _exe(gh, '#!/bin/sh\nprintf \'%s\\n\' "$*" > "$GH_LOG"\n')
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["GH_LOG"] = str(tmp_path / "gh.log")
    script = str(ROOT / "examples/apply/merge_pr.sh")
    missing = subprocess.run([script, str(hermes), "/placeholder/root", "c1", "1"], check=False)
    assert missing.returncode == 2
    completed = subprocess.run(
        [script, str(hermes), "/placeholder/root", "c1", "7", "rebase"],
        check=False,
        env=env,
    )
    assert completed.returncode == 0
    logged = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert logged == f"pr merge 7 --rebase --match-head-commit {SHA}\n"
