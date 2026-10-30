"""Where the shared review documents live, and which of them a review reads."""

import sys
from pathlib import Path

PROFILES = {
    "code": "code.md",
    "design": "design.md",
    "source": "source.md",
}
COMMON = ("README.md", "common.md")


def profile_names(review: str) -> tuple[str, ...]:
    return (*COMMON, PROFILES[review])


def all_names() -> tuple[str, ...]:
    return (*COMMON, *PROFILES.values())


def docs_dir(folder: str) -> Path | None:
    """The source checkout's docs folder, else the one installed with the package."""
    for base in (
        Path(__file__).resolve().parent.parent / "docs" / folder,
        Path(sys.prefix) / "share" / "hermes-taskwatch" / folder,
    ):
        if base.is_dir():
            return base
    return None
