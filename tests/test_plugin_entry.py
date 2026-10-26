"""The Hermes loader finds register(ctx) and the tested version pin."""

from pathlib import Path

from taskwatch.pin import TESTED_HERMES_VERSION
from taskwatch.plugin import register

ROOT = Path(__file__).resolve().parents[1]


def _manifest() -> dict[str, str]:
    found = {}
    for line in (ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith(" ") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        found[key.strip()] = value.strip().strip('"')
    return found


def test_manifest_pins_the_tested_hermes_version():
    manifest = _manifest()
    assert manifest["name"] == "hermes-taskwatch"
    assert manifest["kind"] == "standalone"
    assert manifest["requires_hermes"] == f">={TESTED_HERMES_VERSION}"
    assert TESTED_HERMES_VERSION == "0.21.5"


def test_root_register_loads_under_the_hermes_plugin_namespace():
    """Hermes imports the directory as hermes_plugins.<slug>; the repo is not on sys.path there."""
    import importlib.util
    import sys
    import types

    name = "hermes_plugins.taskwatch_namespace_test"
    sys.modules.setdefault("hermes_plugins", types.ModuleType("hermes_plugins"))
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        assert module.register.__module__ == f"{name}.taskwatch.plugin"
    finally:
        for loaded in [item for item in sys.modules if item.startswith(name)]:
            del sys.modules[loaded]


def test_register_accepts_a_context_without_hermes():
    seen = {}

    class Ctx:
        def register_cli_command(self, **kwargs):
            seen.update(kwargs)

        def register_hook(self, name, callback):
            seen.setdefault("hooks", []).append(name)

    register(Ctx())
    assert seen["name"] == "taskwatch"
    assert seen["hooks"] == ["on_kanban_dispatch_tick", "pre_tool_call"]
    assert seen["setup_fn"].__name__ == "setup_cli"
