"""The plugin loads through Hermes' own loader from an installed directory.

Hermes imports a directory plugin as hermes_plugins.<slug>. The repo is not on sys.path
there, so any absolute `taskwatch` import fails. The child process has no PYTHONPATH and
its working directory holds no copy of the package.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("hermes_cli") is None,
    reason="Hermes is not importable",
)
ROOT = Path(__file__).resolve().parents[1]


def _install(tmp_path: Path, profile: str | None = None) -> tuple[Path, dict[str, str], Path]:
    """The Hermes root, the child environment, and a cwd that holds no copy of the package."""
    root = tmp_path / "home"
    home = root / "profiles" / profile if profile else root
    plugin = home / "plugins" / "hermes-taskwatch"
    plugin.mkdir(parents=True)
    for name in ("__init__.py", "plugin.yaml"):
        shutil.copy2(ROOT / name, plugin / name)
    shutil.copytree(
        ROOT / "taskwatch", plugin / "taskwatch", ignore=shutil.ignore_patterns("__pycache__")
    )
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - hermes-taskwatch\n", encoding="utf-8"
    )
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["HERMES_HOME"] = str(home)
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    return root, env, cwd


def _hermes(env: dict[str, str], cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=120,
    )


def test_installed_plugin_answers_the_taskwatch_command(tmp_path):
    home, env, cwd = _install(tmp_path)
    done = _hermes(env, cwd, "taskwatch", "doctor", "--root", str(home))
    assert "invalid choice" not in done.stderr
    assert "ModuleNotFoundError" not in done.stderr
    # No config was written, so the check runs and fails on that, not on loading.
    assert done.returncode == 1
    assert "config: fail" in done.stdout


def test_the_loader_registers_the_cli_command_without_an_import_error(tmp_path):
    _, env, cwd = _install(tmp_path)
    script = (
        "from hermes_cli.plugins import discover_plugins, get_plugin_manager\n"
        "discover_plugins()\n"
        "mgr = get_plugin_manager()\n"
        "loaded = {k: v.error for k, v in mgr._plugins.items() if 'taskwatch' in k}\n"
        "print(loaded, sorted(mgr._cli_commands))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert "hermes-taskwatch': None" in done.stdout
    assert "taskwatch" in done.stdout.split("}", 1)[1]


def _scan_cards(env: dict[str, str], cwd: Path) -> list[str]:
    script = (
        "from hermes_cli import kanban_db as kb\n"
        "from hermes_cli.kanban_db_connect import connect_closing\n"
        "with connect_closing(kb.kanban_db_path('default')) as conn:\n"
        "    print(json.dumps([task.title for task in kb.list_tasks(conn)]))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", "import json\n" + script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def _scan_as(tmp_path: Path, profile: str) -> list[str]:
    """Scan through Hermes' own entry as ``profile``. The timer runs the same command."""
    from taskwatch.commands import draft_config

    root, env, cwd = _install(tmp_path, profile)
    config = root / "taskwatch" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(draft_config(str(root.resolve()), "default", ("main",))), encoding="utf-8"
    )
    done = _hermes(env, cwd, "taskwatch", "scan")
    assert done.returncode == 0, done.stderr
    return _scan_cards(env, cwd)


def test_scan_runs_as_the_profile_hermes_started_it_in(tmp_path):
    # Hermes takes a `--profile` for itself, so the command cannot carry one. The scan
    # reads its profile from the process and finds that it is the one in the config.
    assert _scan_as(tmp_path / "listed", "main") == []


def test_scan_as_a_profile_the_config_does_not_list_warns_about_the_target(tmp_path):
    # The control for the test above: a mismatch does write a warning card.
    assert _scan_as(tmp_path / "unlisted", "other") == ["target"]
