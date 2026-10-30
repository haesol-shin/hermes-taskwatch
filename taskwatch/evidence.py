"""The review basis."""

from collections.abc import Sequence
from pathlib import Path

from .fingerprint import basis_fingerprint


def basis_of(intent: Path, spec: Path, guidelines: Sequence[Path]) -> str:
    """The review basis. The reviewer and the gate must name the files the same way."""
    named = [("intent", intent), ("spec", spec)]
    named += [(path.name, path) for path in guidelines]
    return basis_fingerprint(named)
