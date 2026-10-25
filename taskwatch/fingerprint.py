"""Result fingerprints and frozen snapshots.

A Git fingerprint is the commit SHA, and only if the work copy has no
uncommitted changes. A non-Git fingerprint is the hash of the sorted
path list, so an add, delete, or rename cannot keep the old value.
freeze writes the approved blobs by object id, so no filter, attribute, or
archive rule of the current work copy can change the bytes.
"""

import hashlib
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_SHA = r"[0-9a-f]{40}"


class FingerprintError(ValueError):
    """The result cannot be identified or frozen."""


@dataclass(frozen=True, slots=True)
class GitResult:
    repo: Path
    sha: str


@dataclass(frozen=True, slots=True)
class FileResult:
    root: Path
    paths: tuple[str, ...]


def git_fingerprint(sha: str) -> str:
    if re.fullmatch(_SHA, sha) is None:
        raise FingerprintError("git fingerprint must be a full lowercase sha")
    return sha


def uncommitted_changes(repo: Path) -> tuple[str, ...]:
    """Porcelain lines, including untracked files.

    A review of a SHA does not cover changes that are not in that commit.
    """
    proc = subprocess.run(
        _git_cmd(
            repo, ["status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"]
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or "git status failed"
        raise FingerprintError(detail)
    return tuple(line for line in proc.stdout.splitlines() if line.strip())


def files_fingerprint(root: Path, relative_paths: Sequence[str]) -> str:
    return _list_hash(_file_rows(root, relative_paths))


def _file_rows(root: Path, relative_paths: Sequence[str]) -> list[tuple[str, str]]:
    # The one place a non-Git source is hashed. The fingerprint and the copy's
    # expected digests both come from this list, so a change between two hashes
    # cannot give the copy a different name than its content.
    return [(_logical(rel), _sha256(_file_under(root, rel))) for rel in relative_paths]


def basis_fingerprint(files: Sequence[tuple[str, Path]]) -> str:
    """Same list hash as a non-Git result, for intent, spec, and guidelines."""
    rows = []
    for name, path in files:
        logical = _logical(name)
        if not path.is_file():
            raise FingerprintError(f"missing basis file: {logical}")
        rows.append((logical, _sha256(path)))
    return _list_hash(rows)


def freeze(result: GitResult | FileResult, state: Path) -> Path:
    if isinstance(result, GitResult):
        return _freeze_git(result, state)
    if isinstance(result, FileResult):
        return _freeze_files(result, state)
    raise FingerprintError("unknown result")


def _freeze_git(result: GitResult, state: Path) -> Path:
    sha = git_fingerprint(result.sha)
    if uncommitted_changes(result.repo):
        raise FingerprintError("uncommitted changes")
    dest = state / "snapshots" / sha
    entries = _commit_entries(result.repo, sha)
    if _snapshot_matches(dest, entries):
        return dest
    _discard(dest)
    dest.mkdir(parents=True)
    try:
        _write_tree(dest, entries)
        if not _symlinks_inside(dest):
            raise FingerprintError("snapshot link escapes the snapshot")
    except Exception:
        _discard(dest)
        raise
    return dest


def _freeze_files(result: FileResult, state: Path) -> Path:
    rows = _file_rows(result.root, result.paths)
    fingerprint = _list_hash(rows)
    expected = dict(rows)
    dest = state / "snapshots" / fingerprint
    if _file_snapshot_ok(dest, expected):
        return dest
    _discard(dest)
    dest.mkdir(parents=True)
    try:
        for rel in expected:
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_file_under(result.root, rel), target)
        if not _file_snapshot_ok(dest, expected):
            raise FingerprintError("snapshot does not match fingerprint")
    except Exception:
        _discard(dest)
        raise
    return dest


def _commit_entries(repo: Path, sha: str) -> dict[str, tuple[str, bytes | None]]:
    rows = _parse_ls_tree(_git_ok(repo, ["ls-tree", "-r", "-z", sha]))
    blobs = [(path, mode, oid) for path, mode, kind, oid in rows if kind == "blob"]
    entries: dict[str, tuple[str, bytes | None]] = {}
    for path, _mode, kind, _oid in rows:
        if kind == "commit":
            entries[path] = ("dir", None)
        elif kind != "blob":
            raise FingerprintError("snapshot does not match fingerprint")
    contents = _blobs(repo, [oid for _path, _mode, oid in blobs])
    for (path, mode, _oid), content in zip(blobs, contents, strict=True):
        entries[path] = (_blob_kind(mode), content)
    _check_layout(entries)
    return entries


def _check_layout(entries: dict[str, tuple[str, bytes | None]]) -> None:
    # A valid tree never has these. With them, a write could pass through a link.
    for rel in entries:
        parts = rel.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise FingerprintError("unsafe path in tree")
        if any("/".join(parts[:i]) in entries for i in range(1, len(parts))):
            raise FingerprintError("unsafe path in tree")


def _blob_kind(mode: str) -> str:
    if mode == "120000":
        return "link"
    if int(mode, 8) & 0o111:
        return "exec"
    return "file"


def _parse_ls_tree(blob: bytes) -> list[tuple[str, str, str, str]]:
    rows = []
    for raw in blob.split(b"\0"):
        if not raw:
            continue
        try:
            meta, path_b = raw.split(b"\t", 1)
            mode, kind, oid = meta.decode("utf-8").split(" ")
            path = path_b.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise FingerprintError("git path is not utf-8") from exc
        rows.append((path, mode, kind, oid))
    return rows


def _blobs(repo: Path, oids: Sequence[str]) -> list[bytes]:
    """Raw blob bytes by object id. No path reaches Git, so no attribute or filter applies."""
    if not oids:
        return []
    payload = "".join(f"{oid}\n" for oid in oids).encode()
    return _parse_batch(_git_ok(repo, ["cat-file", "--batch"], payload), len(oids))


def _parse_batch(blob: bytes, count: int) -> list[bytes]:
    found = []
    pos = 0
    for _ in range(count):
        end = blob.find(b"\n", pos)
        parts = blob[pos:end].split() if end >= 0 else []
        if len(parts) != 3 or parts[1] != b"blob":
            raise FingerprintError("git blob missing")
        start = end + 1
        size = int(parts[2])
        found.append(blob[start : start + size])
        pos = start + size + 1
    return found


def _write_tree(dest: Path, entries: dict[str, tuple[str, bytes | None]]) -> None:
    # Links go last: no earlier write can pass through one.
    links = []
    for rel, (kind, content) in entries.items():
        target = dest / rel
        if kind == "link":
            links.append((target, content))
        elif kind == "dir":
            target.mkdir(parents=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content or b"")
                os.fchmod(handle.fileno(), 0o755 if kind == "exec" else 0o644)
    for target, content in links:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(content or b"", os.fsencode(target))


def _symlinks_inside(dest: Path) -> bool:
    root = os.path.realpath(dest)
    for dirpath, dirnames, filenames in _walk(dest):
        for name in (*dirnames, *filenames):
            path = Path(dirpath) / name
            if path.is_symlink() and not _resolved_inside(root, path):
                return False
    return True


def _resolved_inside(root: str, path: Path) -> bool:
    try:
        resolved = os.path.realpath(path)
        Path(resolved).relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def _snapshot_matches(dest: Path, entries: dict[str, tuple[str, bytes | None]]) -> bool:
    # A symlink root points outside the approved tree even when the bytes match.
    if dest.is_symlink() or not dest.is_dir() or not _symlinks_inside(dest):
        return False
    return _read_snapshot(dest) == entries


def _read_snapshot(dest: Path) -> dict[str, tuple[str, bytes | None]]:
    found: dict[str, tuple[str, bytes | None]] = {}
    for dirpath, dirnames, filenames in _walk(dest):
        base = Path(dirpath)
        link_dirs = []
        for name in list(dirnames):
            path = base / name
            if not path.is_symlink():
                continue
            found[path.relative_to(dest).as_posix()] = ("link", _link_bytes(path))
            dirnames.remove(name)
            link_dirs.append(name)
        for name in filenames:
            found[(base / name).relative_to(dest).as_posix()] = _classify(base / name)
        if base != dest and not dirnames and not filenames and not link_dirs:
            found[base.relative_to(dest).as_posix()] = ("dir", None)
    return found


def _classify(path: Path) -> tuple[str, bytes | None]:
    if path.is_symlink():
        return ("link", _link_bytes(path))
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode):
        return ("other", b"")
    # Git reads only the owner bit. Group or other bits alone cannot run as the
    # approved executable and are not the approved plain mode either.
    if mode & 0o111 and not mode & stat.S_IXUSR:
        return ("other", b"")
    kind = "exec" if mode & stat.S_IXUSR else "file"
    return (kind, path.read_bytes())


def _link_bytes(path: Path) -> bytes:
    return os.readlink(os.fsencode(path))


def _file_snapshot_ok(dest: Path, expected: dict[str, str]) -> bool:
    if dest.is_symlink() or not dest.is_dir():
        return False
    return _snapshot_digests(dest) == expected


def _snapshot_digests(dest: Path) -> dict[str, str]:
    found = {}
    for dirpath, dirnames, filenames in _walk(dest):
        base = Path(dirpath)
        for name in (*dirnames, *filenames):
            path = base / name
            if path.is_dir() and not path.is_symlink():
                continue
            rel = path.relative_to(dest).as_posix()
            if path.is_symlink() or not path.is_file():
                found[rel] = "not-a-file"
            else:
                found[rel] = _sha256(path)
    return found


def _discard(dest: Path) -> None:
    if dest.is_symlink():
        dest.unlink()
        return
    if not dest.exists():
        return
    if not dest.is_dir():
        dest.unlink()
        return
    for dirpath, dirnames, filenames in _walk(dest, topdown=False):
        base = Path(dirpath)
        for name in filenames:
            (base / name).unlink()
        for name in dirnames:
            path = base / name
            if path.is_symlink():
                path.unlink()
            else:
                path.rmdir()
    dest.rmdir()


def _walk(top: Path, topdown: bool = True):
    # os.walk drops a directory it cannot list. That would hide an unapproved
    # file from every comparison, so the error must stop the freeze.
    return os.walk(top, topdown=topdown, followlinks=False, onerror=_raise)


def _raise(error: OSError) -> None:
    raise error


def _git_cmd(repo: Path, args: Sequence[str]) -> list[str]:
    # A replace ref can swap the bytes behind an approved object id.
    return ["git", "--no-replace-objects", "-C", os.fspath(repo.absolute()), *args]


def _git_ok(repo: Path, args: list[str], stdin: bytes | None = None) -> bytes:
    proc = subprocess.run(
        _git_cmd(repo, args),
        input=stdin,
        check=False,
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed"
        raise FingerprintError(detail)
    return proc.stdout


def _list_hash(rows: Sequence[tuple[str, str]]) -> str:
    ordered = sorted(rows)
    if len(ordered) != len(set(ordered)):
        raise FingerprintError("duplicate fingerprint path")
    if len({name for name, _digest in ordered}) != len(ordered):
        raise FingerprintError("duplicate fingerprint path")
    payload = "".join(f"{name}\t{digest}\n" for name, digest in ordered)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _logical(value: str) -> str:
    # Tab and newline separate rows in the list hash. A path that contains a
    # control character can impersonate another list, so those paths are refused
    # instead of length-prefixing every row.
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise FingerprintError(f"path contains a control character: {value}")
    pure = PurePosixPath(value)
    if not value or "\\" in value or pure.is_absolute() or ".." in pure.parts or pure.parts == ():
        raise FingerprintError(f"path escapes root: {value}")
    return pure.as_posix()


def _file_under(root: Path, relative: str) -> Path:
    logical = _logical(relative)
    root_resolved = root.resolve()
    try:
        candidate = (root / logical).resolve()
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise FingerprintError(f"path escapes root: {logical}") from exc
    if not candidate.is_file():
        raise FingerprintError(f"missing output: {logical}")
    return candidate


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
