"""A real Git repository in a temp directory. No network."""

import os
import subprocess
from pathlib import Path


def git(repo: Path, *args: str) -> str:
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


def make_repo(root: Path, name: str = "repo") -> Path:
    repo = root / name
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "out.txt").write_text("one\n", encoding="utf-8")
    git(repo, "add", "out.txt")
    git(repo, "commit", "-m", "add out")
    return repo
