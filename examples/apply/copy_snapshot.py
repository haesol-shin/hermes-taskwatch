"""Copy a frozen snapshot only when what lands is the approved fingerprint.

The snapshot is hashed after it was copied, not before. The copy goes to a temporary
directory next to the destination and is renamed into place only when its own hash
is the approved fingerprint, so a change between a check and the copy cannot land.
"""

import shutil
import sys
import tempfile
from pathlib import Path

# The plugin root holds the `taskwatch` package. The script runs from an installed plugin.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from taskwatch.fingerprint import files_fingerprint  # noqa: E402


def copy_snapshot(source: Path, dest: Path, approved: str) -> None:
    if source.name != approved:
        raise SystemExit("snapshot is not named by the approved fingerprint")
    if dest.exists():
        raise SystemExit("destination exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{dest.name}.", dir=dest.parent))
    try:
        shutil.rmtree(staging)
        shutil.copytree(source, staging)
        if _fingerprint(staging) != approved:
            raise SystemExit("the copy does not match the approved fingerprint")
        staging.rename(dest)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _fingerprint(root: Path) -> str:
    names = tuple(
        sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
    )
    return files_fingerprint(root, names)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Copy a frozen snapshot after a hash check")
    parser.add_argument("source")
    parser.add_argument("dest")
    parser.add_argument("approved")
    args = parser.parse_args(argv)
    copy_snapshot(Path(args.source), Path(args.dest), args.approved)
    return 0


if __name__ == "__main__":
    sys.exit(main())
