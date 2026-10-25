import tomllib
from pathlib import Path

import taskwatch


def test_package_version_matches_pyproject():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert taskwatch.__version__ == pyproject["project"]["version"]
