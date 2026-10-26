import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from taskwatch.fingerprint import (
    FileResult,
    FingerprintError,
    GitResult,
    basis_fingerprint,
    files_fingerprint,
    freeze,
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


def test_freeze_git_writes_the_approved_sha_not_a_dirty_worktree(tmp_path):
    repo = _repo(tmp_path)
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "out.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "out.txt")
    _git(repo, "commit", "-m", "change out")
    state = tmp_path / "state"
    frozen = freeze(GitResult(repo, first), state)
    assert frozen == state / "snapshots" / first
    assert (frozen / "out.txt").read_text(encoding="utf-8") == "one\n"
    (repo / "out.txt").write_text("dirty\n", encoding="utf-8")
    assert (frozen / "out.txt").read_text(encoding="utf-8") == "one\n"
    with pytest.raises(FingerprintError, match="uncommitted"):
        freeze(GitResult(repo, first), state)
    assert (frozen / "out.txt").read_text(encoding="utf-8") == "one\n"


def test_freeze_files_keeps_the_copy_after_the_original_changes(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    result = FileResult(root, ("a.txt",))
    state = tmp_path / "state"
    frozen = freeze(result, state)
    fingerprint = files_fingerprint(root, ["a.txt"])
    assert frozen == state / "snapshots" / fingerprint
    assert freeze(result, state) == frozen
    (root / "a.txt").write_text("changed\n", encoding="utf-8")
    assert (frozen / "a.txt").read_text(encoding="utf-8") == "a\n"
    assert files_fingerprint(root, ["a.txt"]) != fingerprint
    assert files_fingerprint(frozen, ["a.txt"]) == fingerprint


def test_reused_snapshot_must_match_the_approved_files(tmp_path):
    repo = _repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD")
    state = tmp_path / "state"
    frozen = freeze(GitResult(repo, sha), state)
    (frozen / "out.txt").write_text("NOT APPROVED\n", encoding="utf-8")
    again = freeze(GitResult(repo, sha), state)
    assert (again / "out.txt").read_text(encoding="utf-8") == "one\n"


def test_freeze_keeps_export_ignored_and_unsubstituted_blobs(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".gitattributes").write_text(
        "out.txt export-ignore\nversion.txt export-subst\n",
        encoding="utf-8",
    )
    (repo / "version.txt").write_text("$Format:%H$\n", encoding="utf-8")
    _git(repo, "add", ".gitattributes", "version.txt")
    _git(repo, "commit", "-m", "attrs")
    sha = _git(repo, "rev-parse", "HEAD")
    frozen = freeze(GitResult(repo, sha), tmp_path / "state")
    assert (frozen / "out.txt").read_text(encoding="utf-8") == "one\n"
    assert (frozen / "version.txt").read_text(encoding="utf-8") == "$Format:%H$\n"


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


def test_safe_symlink_is_kept_and_escaping_link_is_refused(tmp_path):
    repo = _repo(tmp_path)
    (repo / "current.txt").symlink_to("out.txt")
    _git(repo, "add", "current.txt")
    _git(repo, "commit", "-m", "link")
    sha = _git(repo, "rev-parse", "HEAD")
    frozen = freeze(GitResult(repo, sha), tmp_path / "state")
    assert (frozen / "current.txt").is_symlink()
    assert os.readlink(frozen / "current.txt") == "out.txt"

    (repo / "escape.txt").symlink_to("../out.txt")
    _git(repo, "add", "escape.txt")
    _git(repo, "commit", "-m", "escape")
    bad = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(FingerprintError, match="link"):
        freeze(GitResult(repo, bad), tmp_path / "state")
    assert not (tmp_path / "state" / "snapshots" / bad).exists()


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


def test_reused_snapshot_rejects_a_file_replaced_by_a_symlink(tmp_path):
    repo = _repo(tmp_path)
    (repo / "out.txt").write_text("../outside.txt", encoding="utf-8")
    _git(repo, "add", "out.txt")
    _git(repo, "commit", "-m", "text")
    sha = _git(repo, "rev-parse", "HEAD")
    state = tmp_path / "state"
    frozen = freeze(GitResult(repo, sha), state)
    (frozen / "out.txt").unlink()
    (frozen / "out.txt").symlink_to("../outside.txt")
    (frozen.parent / "outside.txt").write_text("NOT APPROVED\n", encoding="utf-8")
    again = freeze(GitResult(repo, sha), state)
    assert not again.is_symlink()
    assert not (again / "out.txt").is_symlink()
    assert (again / "out.txt").read_text(encoding="utf-8") == "../outside.txt"


def test_reused_snapshot_rejects_a_symlink_replaced_by_a_file(tmp_path):
    repo = _repo(tmp_path)
    (repo / "link.txt").symlink_to("out.txt")
    _git(repo, "add", "link.txt")
    _git(repo, "commit", "-m", "link")
    sha = _git(repo, "rev-parse", "HEAD")
    frozen = freeze(GitResult(repo, sha), tmp_path / "state")
    (frozen / "link.txt").unlink()
    (frozen / "link.txt").write_text("out.txt", encoding="utf-8")
    again = freeze(GitResult(repo, sha), tmp_path / "state")
    assert (again / "link.txt").is_symlink()
    assert os.readlink(again / "link.txt") == "out.txt"


def test_symlink_snapshot_root_is_not_reused(tmp_path):
    repo = _repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD")
    state = tmp_path / "state"
    frozen = freeze(GitResult(repo, sha), state)
    outside = tmp_path / "outside"
    outside.mkdir()
    # Same bytes as the approved tree, so only the link itself can fail the reuse check.
    (outside / "out.txt").write_text("one\n", encoding="utf-8")
    frozen.rename(tmp_path / "real-snap")
    frozen.symlink_to(outside, target_is_directory=True)
    again = freeze(GitResult(repo, sha), state)
    assert not again.is_symlink()
    assert (again / "out.txt").read_text(encoding="utf-8") == "one\n"
    assert (outside / "out.txt").read_text(encoding="utf-8") == "one\n"


def test_intermediate_symlink_cannot_leave_the_snapshot(tmp_path):
    repo = _repo(tmp_path)
    (repo / "d").mkdir()
    (repo / "d" / "a").symlink_to("..")
    (repo / "b").symlink_to("d/a/../../outside")
    _git(repo, "add", "d", "b")
    _git(repo, "commit", "-m", "escape")
    sha = _git(repo, "rev-parse", "HEAD")
    outside = tmp_path / "state" / "outside"
    outside.parent.mkdir(parents=True)
    outside.write_text("ESCAPED\n", encoding="utf-8")
    with pytest.raises(FingerprintError, match="link"):
        freeze(GitResult(repo, sha), tmp_path / "state")
    assert not (tmp_path / "state" / "snapshots" / sha).exists()
    assert outside.read_text(encoding="utf-8") == "ESCAPED\n"


def test_safe_symlink_chain_stays_inside(tmp_path):
    repo = _repo(tmp_path)
    (repo / "d").mkdir()
    (repo / "d" / "a").symlink_to("..")
    (repo / "ok.txt").symlink_to("d/a/out.txt")
    _git(repo, "add", "d", "ok.txt")
    _git(repo, "commit", "-m", "chain")
    sha = _git(repo, "rev-parse", "HEAD")
    frozen = freeze(GitResult(repo, sha), tmp_path / "state")
    assert (frozen / "d" / "a").is_symlink()
    assert (frozen / "ok.txt").is_symlink()
    assert (frozen / "ok.txt").read_text(encoding="utf-8") == "one\n"


def test_freeze_keeps_the_executable_bit(tmp_path):
    repo = _repo(tmp_path)
    script = repo / "run.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    _git(repo, "add", "run.sh")
    _git(repo, "commit", "-m", "script")
    sha = _git(repo, "rev-parse", "HEAD")
    state = tmp_path / "state"
    frozen = freeze(GitResult(repo, sha), state)
    subprocess.run([os.fspath(frozen / "run.sh")], check=True)
    (frozen / "run.sh").chmod(stat.S_IRUSR | stat.S_IWUSR)
    again = freeze(GitResult(repo, sha), state)
    assert (again / "run.sh").stat().st_mode & stat.S_IXUSR
    subprocess.run([os.fspath(again / "run.sh")], check=True)


def test_freeze_keeps_an_empty_gitlink_directory(tmp_path):
    dep = tmp_path / "dep"
    dep.mkdir()
    _git(dep, "init", "-b", "main")
    (dep / "lib.txt").write_text("lib\n", encoding="utf-8")
    _git(dep, "add", "lib.txt")
    _git(dep, "commit", "-m", "dep")
    repo = _repo(tmp_path)
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", os.fspath(dep), "dep")
    _git(repo, "commit", "-m", "add dep")
    sha = _git(repo, "rev-parse", "HEAD")
    frozen = freeze(GitResult(repo, sha), tmp_path / "state")
    assert (frozen / "dep").is_dir()
    assert not (frozen / "dep").is_symlink()
    assert list((frozen / "dep").iterdir()) == []


def test_changed_file_snapshot_content_is_not_reused(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    result = FileResult(root, ("a.txt",))
    copied = freeze(result, tmp_path / "state")
    (copied / "a.txt").write_text("tampered\n", encoding="utf-8")
    reused = freeze(result, tmp_path / "state")
    assert (reused / "a.txt").read_text(encoding="utf-8") == "a\n"


def test_extra_file_snapshot_is_not_reused(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    result = FileResult(root, ("a.txt",))
    copied = freeze(result, tmp_path / "state")
    (copied / "extra.py").write_text("nope\n", encoding="utf-8")
    reused = freeze(result, tmp_path / "state")
    assert (reused / "a.txt").read_text(encoding="utf-8") == "a\n"
    assert not (reused / "extra.py").exists()


def test_file_snapshot_symlink_root_is_not_reused(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    result = FileResult(root, ("a.txt",))
    state = tmp_path / "state"
    copied = freeze(result, state)
    outside = tmp_path / "outside"
    outside.mkdir()
    # Same bytes as the approved file, so only the link itself can fail the reuse check.
    (outside / "a.txt").write_text("a\n", encoding="utf-8")
    copied.rename(tmp_path / "real-files")
    copied.symlink_to(outside, target_is_directory=True)
    again = freeze(result, state)
    assert not again.is_symlink()
    assert (again / "a.txt").read_text(encoding="utf-8") == "a\n"
    assert (outside / "a.txt").read_text(encoding="utf-8") == "a\n"


def _commit_all(repo: Path, message: str, *paths: str) -> str:
    _git(repo, "add", *paths)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def test_eol_attribute_does_not_change_the_frozen_bytes(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".gitattributes").write_text("out.txt text eol=crlf\n", encoding="utf-8")
    (repo / "out.txt").write_bytes(b"ORIGINAL\n")
    sha = _commit_all(repo, "crlf", ".gitattributes", "out.txt")
    state = tmp_path / "state"
    frozen = freeze(GitResult(repo, sha), state)
    assert (frozen / "out.txt").read_bytes() == b"ORIGINAL\n"
    assert freeze(GitResult(repo, sha), state) == frozen


def _filter(repo: Path, name: str, smudge: str) -> None:
    _git(repo, "config", f"filter.{name}.smudge", smudge)
    _git(repo, "config", f"filter.{name}.clean", "cat")


def test_filter_that_differs_between_commits_does_not_change_the_frozen_bytes(tmp_path):
    repo = _repo(tmp_path)
    _filter(repo, "alpha", "tr a-z A-Z")
    _filter(repo, "beta", "tr a-z x")
    (repo / ".gitattributes").write_text("out.txt filter=alpha\n", encoding="utf-8")
    approved = _commit_all(repo, "alpha", ".gitattributes")
    (repo / ".gitattributes").write_text("out.txt filter=beta\n", encoding="utf-8")
    _commit_all(repo, "beta", ".gitattributes")
    frozen = freeze(GitResult(repo, approved), tmp_path / "state")
    assert (frozen / "out.txt").read_bytes() == b"one\n"


def test_filter_on_a_symlink_does_not_change_the_link_target(tmp_path):
    repo = _repo(tmp_path)
    _filter(repo, "swap", "sed s/out/new/")
    (repo / ".gitattributes").write_text("link.txt filter=swap\n", encoding="utf-8")
    (repo / "link.txt").symlink_to("out.txt")
    sha = _commit_all(repo, "link", ".gitattributes", "link.txt")
    frozen = freeze(GitResult(repo, sha), tmp_path / "state")
    assert os.readlink(frozen / "link.txt") == "out.txt"


def test_newline_in_a_file_name_is_kept(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a\nb").write_text("odd\n", encoding="utf-8")
    sha = _commit_all(repo, "newline", "a\nb")
    frozen = freeze(GitResult(repo, sha), tmp_path / "state")
    assert (frozen / "a\nb").read_text(encoding="utf-8") == "odd\n"
    assert (frozen / "out.txt").read_text(encoding="utf-8") == "one\n"


def test_relative_state_path_works_from_another_directory(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    frozen = freeze(GitResult(repo, sha), Path("relative-current"))
    assert (elsewhere / "relative-current" / "snapshots" / sha / "out.txt").read_text(
        encoding="utf-8"
    ) == "one\n"
    assert freeze(GitResult(repo, sha), Path("relative-current")) == frozen


def _object(repo: Path, kind: str, data: bytes) -> str:
    proc = subprocess.run(
        ["git", "-C", os.fspath(repo), "hash-object", "-t", kind, "--literally", "-w", "--stdin"],
        input=data,
        check=True,
        capture_output=True,
    )
    return proc.stdout.decode().strip()


def _tree(repo: Path, rows: list[tuple[str, str, str]]) -> str:
    raw = b"".join(
        f"{mode} {name}".encode() + b"\0" + bytes.fromhex(oid) for mode, name, oid in rows
    )
    return _object(repo, "tree", raw)


def _crafted_commit(repo: Path, tree: str) -> str:
    return _git(repo, "commit-tree", tree, "-m", "crafted")


def test_tree_with_a_dot_dot_entry_is_refused(tmp_path):
    repo = _repo(tmp_path)
    blob = _git(repo, "rev-parse", "HEAD:out.txt")
    sha = _crafted_commit(repo, _tree(repo, [("100644", "..", blob)]))
    with pytest.raises(FingerprintError, match="unsafe path"):
        freeze(GitResult(repo, sha), tmp_path / "state")
    assert not (tmp_path / "state" / "snapshots" / sha).exists()


def test_tree_that_writes_through_a_link_is_refused(tmp_path):
    repo = _repo(tmp_path)
    blob = _git(repo, "rev-parse", "HEAD:out.txt")
    link = _object(repo, "blob", b"../elsewhere")
    inner = _tree(repo, [("100644", "b", blob)])
    tree = _tree(repo, [("120000", "a", link), ("40000", "a", inner)])
    sha = _crafted_commit(repo, tree)
    with pytest.raises(FingerprintError, match="unsafe path"):
        freeze(GitResult(repo, sha), tmp_path / "state")
    assert not (tmp_path / "state" / "snapshots" / sha).exists()


def test_reused_git_snapshot_rejects_an_extra_file(tmp_path):
    repo = _repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD")
    state = tmp_path / "state"
    frozen = freeze(GitResult(repo, sha), state)
    (frozen / "extra.py").write_text("print('not approved')\n", encoding="utf-8")
    again = freeze(GitResult(repo, sha), state)
    assert not (again / "extra.py").exists()


def _file_result(tmp_path: Path) -> FileResult:
    root = tmp_path / "out"
    root.mkdir()
    (root / "a.txt").write_text("approved\n", encoding="utf-8")
    return FileResult(root, ("a.txt",))


@pytest.mark.parametrize("moment", ["after-hash", "during-copy"])
def test_source_that_changes_before_the_copy_ends_is_not_frozen(tmp_path, monkeypatch, moment):
    import taskwatch.fingerprint as module

    result = _file_result(tmp_path)
    source = result.root / "a.txt"
    real_hash, real_copy = module._sha256, shutil.copy2
    hashed = {"n": 0}

    def sha256(path):
        digest = real_hash(path)
        hashed["n"] += 1
        if moment == "after-hash" and hashed["n"] == 1:
            source.write_text("NOT APPROVED\n", encoding="utf-8")
        return digest

    def copy2(src, dst, **kwargs):
        if moment == "during-copy":
            source.write_text("NOT APPROVED\n", encoding="utf-8")
        return real_copy(src, dst, **kwargs)

    monkeypatch.setattr(module, "_sha256", sha256)
    monkeypatch.setattr(shutil, "copy2", copy2)
    state = tmp_path / "state"
    with pytest.raises(FingerprintError, match="does not match"):
        freeze(result, state)
    assert list((state / "snapshots").iterdir()) == []


def test_replaced_blob_does_not_change_the_frozen_bytes(tmp_path):
    repo = _repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD")
    approved = _git(repo, "rev-parse", "HEAD:out.txt")
    _git(repo, "replace", approved, _object(repo, "blob", b"NOT APPROVED\n"))
    frozen = freeze(GitResult(repo, sha), tmp_path / "state")
    assert (frozen / "out.txt").read_bytes() == b"one\n"


def test_replaced_tree_does_not_change_the_frozen_files(tmp_path):
    repo = _repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    other = _object(repo, "blob", b"NOT APPROVED\n")
    fake = _tree(repo, [("100644", "extra.txt", other), ("100644", "out.txt", other)])
    _git(repo, "replace", tree, fake)
    frozen = freeze(GitResult(repo, sha), tmp_path / "state")
    assert sorted(path.name for path in frozen.iterdir()) == ["out.txt"]
    assert (frozen / "out.txt").read_bytes() == b"one\n"


@pytest.mark.parametrize("mode", [0o655, 0o611, 0o455])
def test_executable_copy_without_the_owner_bit_is_rebuilt(tmp_path, mode):
    repo = _repo(tmp_path)
    (repo / "run.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (repo / "run.sh").chmod(0o755)
    sha = _commit_all(repo, "script", "run.sh")
    state = tmp_path / "state"
    frozen = freeze(GitResult(repo, sha), state)
    (frozen / "run.sh").chmod(mode)
    again = freeze(GitResult(repo, sha), state)
    subprocess.run([os.fspath(again / "run.sh")], check=True)


@pytest.mark.parametrize("mode", [0o744, 0o654, 0o601])
def test_plain_copy_with_any_execute_bit_is_rebuilt(tmp_path, mode):
    repo = _repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD")
    state = tmp_path / "state"
    frozen = freeze(GitResult(repo, sha), state)
    (frozen / "out.txt").chmod(mode)
    again = freeze(GitResult(repo, sha), state)
    assert (again / "out.txt").stat().st_mode & 0o111 == 0


def _deny_listing(monkeypatch, name: str) -> None:
    real = os.scandir

    def scandir(path="."):
        if os.path.basename(os.fspath(path)) == name:
            raise PermissionError(13, "denied", os.fspath(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)


def test_unreadable_directory_in_a_git_snapshot_fails_the_freeze(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD")
    state = tmp_path / "state"
    frozen = freeze(GitResult(repo, sha), state)
    (frozen / "extra").mkdir()
    (frozen / "extra" / "bad.py").write_text("print('not approved')\n", encoding="utf-8")
    _deny_listing(monkeypatch, "extra")
    with pytest.raises(PermissionError):
        freeze(GitResult(repo, sha), state)


def test_unreadable_directory_in_a_file_snapshot_fails_the_freeze(tmp_path, monkeypatch):
    result = _file_result(tmp_path)
    state = tmp_path / "state"
    copied = freeze(result, state)
    (copied / "extra").mkdir()
    (copied / "extra" / "bad.py").write_text("print('not approved')\n", encoding="utf-8")
    _deny_listing(monkeypatch, "extra")
    with pytest.raises(PermissionError):
        freeze(result, state)


class _Spy(bytes):
    """Counts the bytes that slicing and partitioning copy out of it."""

    copied = 0

    def __getitem__(self, key):
        out = bytes.__getitem__(self, key)
        if isinstance(key, slice):
            _Spy.copied += len(out)
            return _Spy(out)
        return out

    def partition(self, sep):
        head, mid, tail = bytes.partition(self, sep)
        _Spy.copied += len(head) + len(tail)
        return _Spy(head), mid, _Spy(tail)


def test_batch_output_is_parsed_without_copying_the_rest_per_blob():
    from taskwatch.fingerprint import _parse_batch

    blobs = [bytes([index]) * 4096 for index in range(64)]
    raw = b"".join(f"{'a' * 40} blob {len(data)}\n".encode() + data + b"\n" for data in blobs)
    _Spy.copied = 0
    assert _parse_batch(_Spy(raw), len(blobs)) == blobs
    assert _Spy.copied <= 2 * len(raw)
