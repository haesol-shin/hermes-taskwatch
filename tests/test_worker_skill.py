"""The example skill must keep the worker on the foreground path."""

from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "examples/skills/taskwatch-worker/SKILL.md"


def test_worker_skill_names_the_foreground_path_and_the_three_calls():
    text = SKILL.read_text(encoding="utf-8")
    assert "foreground" in text
    assert "run_executor.py" in text
    assert "JSON line" in text
    for name in ("kanban_complete", "kanban_request_review", "kanban_block"):
        assert name in text
    assert "Do not background" in text


def test_worker_skill_describes_the_record_the_script_writes_and_the_verdict_line():
    text = SKILL.read_text(encoding="utf-8")
    assert "--result" in text
    assert "--checklist" in text
    assert "approved snapshot" in text
    assert '"verdict"' in text
