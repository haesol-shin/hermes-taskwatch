"""The reviewer launcher builds the omp command from an agent file and the guideline folder."""

import os
import shlex
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from taskwatch import reviewer_launch
from taskwatch.job_runner import _guidelines
from taskwatch.review_docs import docs_dir, profile_names
from taskwatch.reviewer_launch import (
    LaunchError,
    _guideline_files,
    disabled_agents,
    main,
    overlay_text,
    parse_agent,
    plan,
    system_prompt,
)

AGENT = """---
name: reviewer
description: "Read-only review"
tools: read, find, grep, bash
spawns: scout
model: "@slow"
thinking-level: high
autoloadSkills: [review-core]
output:
  properties:
    overall_correctness:
      enum: [correct, incorrect]
---

Find every problem. Read-only.
"""

FAKE_OMP = f"""#!{sys.executable}
import json, sys
from pathlib import Path
args = sys.argv[1:]
assert args[:2] == ["agents", "unpack"], args
target = Path(args[args.index("--dir") + 1])
for name in ("task", "scout", "reviewer", "sonic"):
    (target / f"{{name}}.md").write_text(f"---\\nname: {{name}}\\ndescription: d\\n---\\nbody\\n")
print(json.dumps({{"total": 4}}))
"""


@pytest.fixture
def omp(tmp_path, monkeypatch) -> str:
    path = tmp_path / "bin" / "fake-omp"
    path.parent.mkdir()
    path.write_text(FAKE_OMP, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent-dir"))
    monkeypatch.chdir(tmp_path)
    return str(path)


@pytest.fixture
def guidelines(tmp_path) -> Path:
    folder = tmp_path / "guidelines"
    folder.mkdir()
    for name in (*profile_names("code"), "design.md", "source-checked-document.md"):
        (folder / name).write_text(f"rule text of {name}\n", encoding="utf-8")
    return folder


def agent_file(tmp_path: Path, text: str = AGENT) -> Path:
    path = tmp_path / "reviewer.md"
    path.write_text(text, encoding="utf-8")
    return path


def command(capsys, *args: str) -> list[str]:
    assert main([*args, "--print-command"]) == 0
    return shlex.split(capsys.readouterr().out)


# --- agent definition -----------------------------------------------------------------------


def test_front_matter_gives_tools_spawns_model_and_body():
    agent = parse_agent(AGENT)
    assert agent.tools == ("read", "find", "grep", "bash")
    assert (agent.spawns, agent.models, agent.thinking) == (("scout",), ("@slow",), "high")
    assert agent.body == "Find every problem. Read-only.\n"


@pytest.mark.parametrize(
    "text, spawns",
    [("spawns: '*'\n", None), ("spawns: [scout, spec]\n", ("scout", "spec")), ("", ())],
)
def test_spawns_forms(text, spawns):
    assert parse_agent(f"---\nname: r\ntools: []\n{text}---\nbody\n").spawns == spawns


@pytest.mark.parametrize(
    "text", ["no front matter\n", "---\nname: r\nbody\n", "---\ntools: read\n---\nx\n"]
)
def test_a_definition_without_usable_front_matter_is_refused(text):
    with pytest.raises(LaunchError):
        parse_agent(text)


# --- the command ----------------------------------------------------------------------------


def built(tmp_path, agent_text=AGENT, known=("task", "scout", "reviewer", "spec"), layer=None):
    agent = parse_agent(agent_text)
    files = [("common.md", "common text\n"), ("code.md", "code text\n")]
    return plan(agent, files, layer, set(known), "omp", tmp_path / "work")


def test_command_carries_tools_model_prompt_and_overlay(tmp_path):
    built_plan = built(tmp_path)
    argv = list(built_plan.argv)
    assert argv[0] == "omp"
    assert argv[argv.index("--model") + 1] == "@slow"
    assert argv[argv.index("--thinking") + 1] == "high"
    # Spawning needs the task tool even when the definition does not list it.
    assert argv[argv.index("--tools") + 1] == "read,find,grep,bash,task"
    assert argv[argv.index("--append-system-prompt") + 1] == str(built_plan.system_path)
    assert argv[argv.index("--config") + 1] == str(built_plan.overlay_path)


def test_overlay_turns_async_off_and_disables_every_agent_not_allowed():
    agent = parse_agent(AGENT)
    assert disabled_agents(agent, {"task", "scout", "reviewer", "spec"}) == [
        "reviewer",
        "spec",
        "task",
    ]
    assert overlay_text(["reviewer", "task"]) == (
        'async:\n  enabled: false\ntask:\n  disabledAgents: ["reviewer", "task"]\n'
    )


def test_wildcard_spawns_disable_nothing_and_no_spawns_drops_the_task_tool(tmp_path):
    wildcard = built(tmp_path, AGENT.replace("spawns: scout", "spawns: '*'"))
    assert wildcard.overlay.endswith("disabledAgents: []\n")
    none = built(tmp_path, AGENT.replace("spawns: scout\n", ""))
    assert "task" not in none.argv[list(none.argv).index("--tools") + 1].split(",")
    assert '"scout"' in none.overlay


def test_prompt_is_body_then_each_guideline_in_full_then_the_layer():
    agent = parse_agent(AGENT)
    text = system_prompt(agent, [("common.md", "one\n"), ("code.md", "two\n")], "layer\n")
    assert text.index("Find every problem") < text.index("one") < text.index("two")
    assert text.index("two") < text.index("layer")
    assert '<guideline file="common.md">\none\n</guideline>' in text
    assert "review-core" not in text


def test_editing_a_guideline_changes_the_next_command_and_files(tmp_path):
    first = plan(parse_agent(AGENT), [("c.md", "v1\n")], None, set(), "omp", tmp_path)
    again = plan(parse_agent(AGENT), [("c.md", "v1\n")], None, set(), "omp", tmp_path)
    second = plan(parse_agent(AGENT), [("c.md", "v2\n")], None, set(), "omp", tmp_path)
    assert first.argv == again.argv
    assert first.system_path != second.system_path
    reviewer_launch.materialize(second)
    assert "v2" in second.system_path.read_text(encoding="utf-8")


# --- the program ----------------------------------------------------------------------------


def test_main_reads_the_folder_at_run_time_and_passes_other_options_to_omp(
    tmp_path, guidelines, omp, capsys
):
    base = ["--agent", str(agent_file(tmp_path)), "--guidelines", str(guidelines)]
    base += ["--omp", omp, "--work-dir", str(tmp_path / "work"), "--kind", "code"]
    argv = command(capsys, *base, "--mode", "rpc", "--no-ui", "-p", "go")
    assert argv[0] == omp and argv[1:6] == ["--mode", "rpc", "--no-ui", "-p", "go"]
    # Built options follow the passthrough, so the overlay is the last one omp reads.
    assert argv[-2] == "--config" and argv[-1].endswith("overlay.yml")
    system = Path(argv[argv.index("--append-system-prompt") + 1])
    assert "rule text of common.md" in system.read_text(encoding="utf-8")
    assert "rule text of design.md" not in system.read_text(encoding="utf-8")
    overlay = Path(argv[argv.index("--config") + 1]).read_text(encoding="utf-8")
    # Bundled agents come from omp itself, and the allowed one stays enabled.
    assert '"reviewer", "sonic", "task"' in overlay

    (guidelines / "common.md").write_text("EDITED RULE\n", encoding="utf-8")
    after = command(capsys, *base)
    assert "EDITED RULE" in Path(after[after.index("--append-system-prompt") + 1]).read_text(
        encoding="utf-8"
    )


def test_user_and_project_agents_are_disabled_too(tmp_path, guidelines, omp, capsys):
    user = tmp_path / "agent-dir" / "agents"
    user.mkdir(parents=True)
    (user / "spec.md").write_text("---\nname: spec\ndescription: d\n---\nx\n", encoding="utf-8")
    project = tmp_path / ".omp" / "agents"
    project.mkdir(parents=True)
    (project / "helper.md").write_text(
        "---\nname: helper\ndescription: d\n---\nx\n", encoding="utf-8"
    )
    argv = command(
        capsys,
        "--agent",
        str(agent_file(tmp_path)),
        "--guidelines",
        str(guidelines),
        "--omp",
        omp,
        "--work-dir",
        str(tmp_path / "work"),
    )
    overlay = Path(argv[argv.index("--config") + 1]).read_text(encoding="utf-8")
    assert '"helper"' in overlay and '"spec"' in overlay and '"scout"' not in overlay


def test_main_without_print_command_replaces_itself_with_omp(
    tmp_path, guidelines, omp, monkeypatch
):
    seen = []
    monkeypatch.setattr(os, "execvp", lambda file, argv: seen.append((file, argv)))
    args = ["--agent", str(agent_file(tmp_path)), "--guidelines", str(guidelines), "--omp", omp]
    assert main([*args, "--work-dir", str(tmp_path / "work"), "-p", "go"]) == 0
    assert seen[0][0] == omp and seen[0][1][1:3] == ["-p", "go"]


@pytest.mark.parametrize(
    "extra, message",
    [
        (["--kind", "poetry"], "unknown review kind"),
        (["--guidelines", "{tmp}/nowhere"], "unreadable"),
        (["--agent", "{tmp}/missing.md"], "unreadable"),
    ],
)
def test_bad_input_stops_before_omp_runs(
    tmp_path, guidelines, omp, capsys, monkeypatch, extra, message
):
    monkeypatch.setattr(os, "execvp", lambda *a: pytest.fail("omp must not start"))
    args = ["--agent", str(agent_file(tmp_path)), "--guidelines", str(guidelines), "--omp", omp]
    args += [part.replace("{tmp}", str(tmp_path)) for part in extra]
    assert main(args) == 1
    assert message in capsys.readouterr().err


def test_a_failing_omp_listing_stops_the_launch(tmp_path, guidelines, capsys, monkeypatch):
    broken = tmp_path / "broken"
    broken.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    broken.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    args = [
        "--agent",
        str(agent_file(tmp_path)),
        "--guidelines",
        str(guidelines),
        "--omp",
        str(broken),
    ]
    assert main([*args, "--work-dir", str(tmp_path / "w")]) == 1
    assert "bundled agents" in capsys.readouterr().err


def test_the_shipped_guidelines_and_launch_layers_load_for_every_kind():
    folder = docs_dir("review")
    for kind in ("code", "design", "document", "research"):
        names = [name for name, _ in _guideline_files(folder, [kind])]
        assert names == list(profile_names(kind))
    assert [n for n, _ in _guideline_files(folder, [])][-3:] == [
        "code.md",
        "design.md",
        "source-checked-document.md",
    ]
    for layer in ("taskwatch.md", "omp-alone.md"):
        assert (docs_dir("launch") / layer).read_text(encoding="utf-8").strip()


def test_the_job_copy_and_the_source_folder_give_the_same_guideline_text(tmp_path):
    copy = tmp_path / "job"
    copy.mkdir()
    _guidelines(SimpleNamespace(kind="code"), copy)  # type: ignore[arg-type]
    source = _guideline_files(docs_dir("review"), ["code"])
    assert _guideline_files(copy / "review-docs", ["code"]) == source


BLOCK_AGENT = """---
name: reviewer
tools:
  - read
  - grep
spawns:
- scout
model:
  - "@slow"
---
body
"""


def test_block_lists_from_omp_unpack_are_read():
    agent = parse_agent(BLOCK_AGENT)
    assert (agent.tools, agent.spawns, agent.models) == (("read", "grep"), ("scout",), ("@slow",))


@pytest.mark.parametrize(
    "text", ["tools:\nspawns: scout\n", "tools: |\n  read\n", "tools: read\nmodel:\n"]
)
def test_a_list_the_launcher_cannot_read_is_refused_not_dropped(text):
    with pytest.raises(LaunchError, match="no value"):
        parse_agent(f"---\nname: r\n{text}---\nbody\n")


def test_missing_tools_is_refused_and_empty_tools_means_no_tools(tmp_path):
    with pytest.raises(LaunchError, match="must list its tools"):
        parse_agent("---\nname: r\n---\nbody\n")
    none = built(tmp_path, "---\nname: r\ntools: []\nspawns: []\n---\nbody\n")
    assert "--no-tools" in none.argv and "--tools" not in none.argv
    spawning = built(tmp_path, "---\nname: r\ntools: []\nspawns: scout\n---\nbody\n")
    assert spawning.argv[list(spawning.argv).index("--tools") + 1] == "task"


def test_extensions_are_off(tmp_path):
    assert "--no-extensions" in built(tmp_path).argv


@pytest.mark.parametrize(
    "extra", [["--config", "x.yml"], ["--tools=edit"], ["--no-tools"], ["-e", "ext.ts"]]
)
def test_options_that_would_widen_the_review_are_refused(
    tmp_path, guidelines, omp, capsys, monkeypatch, extra
):
    monkeypatch.setattr(os, "execvp", lambda *a: pytest.fail("omp must not start"))
    args = ["--agent", str(agent_file(tmp_path)), "--guidelines", str(guidelines), "--omp", omp]
    assert main([*args, "--work-dir", str(tmp_path / "w"), *extra]) == 1
    assert "would widen" in capsys.readouterr().err


def test_agents_of_parent_projects_and_named_extras_are_disabled(tmp_path, guidelines, omp, capsys):
    parent = tmp_path / ".omp" / "agents"
    parent.mkdir(parents=True)
    (parent / "up.md").write_text("---\nname: up\ndescription: d\n---\nx\n", encoding="utf-8")
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    os.chdir(deep)
    args = ["--agent", str(agent_file(tmp_path)), "--guidelines", str(guidelines), "--omp", omp]
    argv = command(
        capsys, *args, "--work-dir", str(tmp_path / "w"), "--disable-agent", "from-plugin"
    )
    overlay = Path(argv[argv.index("--config") + 1]).read_text(encoding="utf-8")
    assert '"up"' in overlay and '"from-plugin"' in overlay and '"scout"' not in overlay
