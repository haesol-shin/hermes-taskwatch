import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from taskwatch.fingerprint import (
    FingerprintError,
    basis_fingerprint,
    files_fingerprint,
    git_fingerprint,
    uncommitted_changes,
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        [
            "git",
            "-C",
            os.fspath(repo),
            "-c",
            "user.name=Dev",
            "-c",
            "user.email=dev@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "out.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "out.txt")
    _git(repo, "commit", "-m", "add out")
    return repo


def test_git_fingerprint_is_the_full_lowercase_sha():
    sha = "a" * 40
    assert git_fingerprint(sha) == sha
    with pytest.raises(FingerprintError):
        git_fingerprint("A" * 40)
    with pytest.raises(FingerprintError):
        git_fingerprint("a" * 12)


def test_uncommitted_changes_include_edits_and_untracked(tmp_path):
    repo = _repo(tmp_path)
    assert uncommitted_changes(repo) == ()
    (repo / "out.txt").write_text("two\n", encoding="utf-8")
    assert uncommitted_changes(repo)
    _git(repo, "checkout", "--", "out.txt")
    (repo / "new.txt").write_text("x\n", encoding="utf-8")
    assert any("new.txt" in line for line in uncommitted_changes(repo))
    _git(repo, "add", "new.txt")
    assert uncommitted_changes(repo)


def test_file_list_hash_changes_on_add_delete_rename_and_content(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    (root / "b.txt").write_text("b\n", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "c.txt").write_text("c\n", encoding="utf-8")
    paths = ["b.txt", "a.txt", "sub/c.txt"]
    first = files_fingerprint(root, paths)
    assert first == files_fingerprint(root, list(reversed(paths)))

    (root / "a.txt").write_text("changed\n", encoding="utf-8")
    changed = files_fingerprint(root, paths)
    assert changed != first
    (root / "a.txt").write_text("a\n", encoding="utf-8")

    with pytest.raises(FingerprintError, match="duplicate"):
        files_fingerprint(root, [*paths, "sub/c.txt"])
    (root / "extra.txt").write_text("e\n", encoding="utf-8")
    assert files_fingerprint(root, [*paths, "extra.txt"]) != first
    assert files_fingerprint(root, ["a.txt", "sub/c.txt"]) != first
    (root / "sub" / "d.txt").write_text("c\n", encoding="utf-8")
    assert files_fingerprint(root, ["a.txt", "b.txt", "sub/d.txt"]) != first
    with pytest.raises(FingerprintError, match="missing output"):
        files_fingerprint(root, ["gone.txt"])


def test_path_escape_is_refused(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("no\n", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)
    with pytest.raises(FingerprintError, match="escapes"):
        files_fingerprint(root, ["../secret.txt"])
    with pytest.raises(FingerprintError, match="escapes"):
        files_fingerprint(root, ["link.txt"])


def test_basis_uses_the_same_list_hash(tmp_path):
    intent = tmp_path / "intent.md"
    spec = tmp_path / "spec.md"
    guide = tmp_path / "guide.md"
    for path, text in ((intent, "intent\n"), (spec, "spec\n"), (guide, "guide\n")):
        path.write_text(text, encoding="utf-8")
    files = [("spec.md", spec), ("intent.md", intent), ("guide.md", guide)]
    first = basis_fingerprint(files)
    assert first == basis_fingerprint(list(reversed(files)))
    spec.write_text("spec changed\n", encoding="utf-8")
    assert basis_fingerprint(files) != first
    spec.write_text("spec\n", encoding="utf-8")
    renamed = [("intent.md", intent), ("spec.md", spec), ("guides/code.md", guide)]
    assert basis_fingerprint(renamed) != first
    with pytest.raises(FingerprintError, match="missing basis"):
        basis_fingerprint([("intent.md", tmp_path / "missing.md")])


def test_submodule_ignore_does_not_hide_dirty_files(tmp_path):
    dep = tmp_path / "dep"
    dep.mkdir()
    _git(dep, "init", "-b", "main")
    (dep / "lib.txt").write_text("lib\n", encoding="utf-8")
    _git(dep, "add", "lib.txt")
    _git(dep, "commit", "-m", "dep")
    repo = _repo(tmp_path)
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", os.fspath(dep), "dep")
    _git(repo, "commit", "-m", "add dep")
    _git(repo, "config", "submodule.dep.ignore", "all")
    (repo / "dep" / "lib.txt").write_text("changed\n", encoding="utf-8")
    assert any("dep" in line for line in uncommitted_changes(repo))


def test_control_characters_cannot_share_a_path_list_fingerprint(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    digest = hashlib.sha256(b"same").hexdigest()
    (root / "a").write_bytes(b"same")
    (root / "b").write_bytes(b"same")
    weird = "a\t" + digest + "\nb"
    (root / weird).write_bytes(b"same")
    with pytest.raises(FingerprintError, match="control"):
        files_fingerprint(root, [weird])
    plain = tmp_path / "basis.txt"
    plain.write_bytes(b"same")
    with pytest.raises(FingerprintError, match="control"):
        basis_fingerprint([("a\nb", plain)])
