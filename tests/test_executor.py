"""The executor runs the template as an argument list and records what really happened."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from taskwatch.config import load_config
from taskwatch.evidence import EvidenceBoard, basis_of, load_runs
from taskwatch.executor import run_executor
from taskwatch.fingerprint import files_fingerprint
from taskwatch.protocol import Card
from taskwatch.state import State
from tests.config_fixture import write_config
from tests.fake_board import FakeBoard
from tests.git_fixture import git, make_repo
from tests.threads_fixture import in_threads

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)
VERDICT_PASS = '{"verdict": "PASS", "blocking": false, "reviewer": "reviewer.invalid"}'
VERDICT_FAIL = '{"verdict": "FAIL", "blocking": true, "reviewer": "reviewer.invalid"}'


def _config(tmp_path):
    root = tmp_path / "hermes"
    root.mkdir()
    raw_path = write_config(root / "taskwatch" / "config.json")
    return load_config(raw_path), State(root)


def _basis_files(tmp_path):
    intent = tmp_path / "intent.md"
    spec = tmp_path / "spec.md"
    intent.write_text("why\n", encoding="utf-8")
    spec.write_text("what\n", encoding="utf-8")
    return intent, spec


def _no_run(argv, log):
    raise AssertionError(f"the executor must not start: {argv}")


def _review_kwargs(tmp_path, work, **extra):
    intent, spec = _basis_files(tmp_path)
    kwargs = {
        "kind": "review",
        "card": "c1",
        "brief": str(intent),
        "copy": str(work),
        "output": str(tmp_path / "review.md"),
        "now": NOW,
        "intent": intent,
        "spec": spec,
        "results": ("out.txt",),
    }
    kwargs.update(extra)
    return kwargs


def _writes(path: Path, text: str, code: int = 0):
    def runner(argv, log):
        path.write_text(text, encoding="utf-8")
        return code

    return runner


def _work(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "out.txt").write_text("result\n", encoding="utf-8")
    return work


def test_review_refuses_uncommitted_git_changes_before_the_executor(tmp_path):
    config, state = _config(tmp_path)
    repo = make_repo(tmp_path)
    sha = git(repo, "rev-parse", "HEAD")
    (repo / "out.txt").write_text("edited\n", encoding="utf-8")
    result = run_executor(
        config, state=state, runner=_no_run, git=True, sha=sha, **_review_kwargs(tmp_path, repo)
    )
    assert result == {"ok": False, "block": "uncommitted changes"}
    assert load_runs(state) == ()


def test_review_refuses_a_sha_that_is_not_the_copy_head(tmp_path):
    config, state = _config(tmp_path)
    repo = make_repo(tmp_path)
    reviewed = git(repo, "rev-parse", "HEAD")
    (repo / "out.txt").write_text("next\n", encoding="utf-8")
    git(repo, "commit", "-am", "next")
    result = run_executor(
        config,
        state=state,
        runner=_no_run,
        git=True,
        sha=reviewed,
        **_review_kwargs(tmp_path, repo),
    )
    assert result["ok"] is False
    assert "not the reviewed sha" in result["block"]
    assert state.load().reviews == ()


def test_git_review_records_the_verified_sha(tmp_path):
    config, state = _config(tmp_path)
    repo = make_repo(tmp_path)
    sha = git(repo, "rev-parse", "HEAD")
    kwargs = _review_kwargs(tmp_path, repo)
    result = run_executor(
        config,
        state=state,
        runner=_writes(Path(kwargs["output"]), f"ok\n{VERDICT_PASS}\n"),
        git=True,
        sha=sha,
        **kwargs,
    )
    assert result["ok"] is True
    assert state.load().reviews[0].fingerprint == sha


def test_review_fails_when_the_copy_moves_during_the_run(tmp_path):
    config, state = _config(tmp_path)
    repo = make_repo(tmp_path)
    sha = git(repo, "rev-parse", "HEAD")
    kwargs = _review_kwargs(tmp_path, repo)

    def moving(argv, log):
        Path(kwargs["output"]).write_text(VERDICT_PASS + "\n", encoding="utf-8")
        (repo / "out.txt").write_text("moved\n", encoding="utf-8")
        git(repo, "commit", "-am", "moved")
        return 0

    result = run_executor(config, state=state, runner=moving, git=True, sha=sha, **kwargs)
    assert result["ok"] is False
    assert "changed during the review" in result["block"]
    assert state.load().reviews == ()


def test_work_keeps_a_spaced_path_as_one_argument_and_records_the_exit_code(tmp_path):
    config, state = _config(tmp_path)
    work = _work(tmp_path)
    seen = []

    def runner(argv, log):
        seen.append(argv)
        return 2

    result = run_executor(
        config,
        kind="work",
        card="c1",
        brief="/placeholder/my brief.md",
        copy=str(work),
        output="/placeholder/out",
        now=NOW,
        state=state,
        runner=runner,
        results=("out.txt",),
    )
    assert isinstance(seen[0], list)
    assert "/placeholder/my brief.md" in seen[0]
    assert not any(" " in part and part != "/placeholder/my brief.md" for part in seen[0])
    assert result["ok"] is False
    assert result["exit_code"] == 2
    assert load_runs(state)[0].exit_code == 2


def test_paths_are_recorded_absolute_so_the_gate_reads_them_from_any_cwd(tmp_path, monkeypatch):
    config, state = _config(tmp_path)
    work = _work(tmp_path)
    (tmp_path / "check.json").write_text('{"items": []}', encoding="utf-8")
    (tmp_path / "progress.md").write_text("done\n", encoding="utf-8")
    guideline = tmp_path / "rules.md"
    guideline.write_text("rules\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    worked = run_executor(
        config,
        kind="work",
        card="c1",
        brief="brief",
        copy="work",
        output="out",
        now=NOW,
        state=state,
        runner=lambda argv, log: 0,
        results=("out.txt",),
        checklist="check.json",
        progress="progress.md",
    )
    kwargs = _review_kwargs(
        tmp_path, work, copy="work", output="review.md", guidelines=(Path("rules.md"),)
    )
    reviewed = run_executor(
        config, state=state, runner=_writes(tmp_path / "review.md", VERDICT_PASS + "\n"), **kwargs
    )
    assert worked["ok"] is True
    assert reviewed["ok"] is True
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    intent, spec = kwargs["intent"], kwargs["spec"]
    card = Card(id="c1", status="review", intent_path=str(intent), spec_path=str(spec))
    seen = EvidenceBoard(FakeBoard((card,)), state).card("c1")
    assert seen.fingerprint == files_fingerprint(work, ("out.txt",))
    assert seen.checklist_path == str(tmp_path / "check.json")
    assert seen.progress_path == str(tmp_path / "progress.md")
    assert seen.basis == basis_of(intent, spec, (guideline,))
    assert all(Path(run.copy).is_absolute() for run in load_runs(state))


def test_a_review_run_that_writes_nothing_does_not_reuse_an_earlier_body(tmp_path):
    config, state = _config(tmp_path)
    work = _work(tmp_path)
    kwargs = _review_kwargs(tmp_path, work)
    output = Path(kwargs["output"])
    output.write_text(f"# Earlier review\n{VERDICT_PASS}\n", encoding="utf-8")
    result = run_executor(config, state=state, runner=lambda argv, log: 0, **kwargs)
    assert result["ok"] is False
    assert "no output" in result["block"]
    assert state.load().reviews == ()
    # The earlier body is kept, under another name.
    (kept,) = tmp_path.glob("review.md.before-*")
    assert VERDICT_PASS in kept.read_text(encoding="utf-8")
    fresh = run_executor(config, state=state, runner=_writes(output, VERDICT_FAIL + "\n"), **kwargs)
    assert fresh["verdict"] == "FAIL"
    assert [item.verdict for item in state.load().reviews] == ["FAIL"]


def test_work_with_a_missing_required_output_is_a_block(tmp_path):
    config, state = _config(tmp_path)
    work = _work(tmp_path)
    result = run_executor(
        config,
        kind="work",
        card="c1",
        brief="brief",
        copy=str(work),
        output="out",
        now=NOW,
        state=state,
        runner=lambda argv, log: 0,
        results=("out.txt", "report.md"),
    )
    assert result["ok"] is False
    assert "report.md" in result["block"]
    assert "out.txt" not in result["block"]
    assert result["exit_code"] == 0


def test_work_must_name_a_required_result_unless_it_is_a_git_copy(tmp_path):
    config, state = _config(tmp_path)
    result = run_executor(
        config,
        kind="work",
        card="c1",
        brief="b",
        copy=str(tmp_path),
        output="o",
        now=NOW,
        state=state,
        runner=_no_run,
    )
    assert result["ok"] is False
    assert load_runs(state) == ()


def test_review_records_the_verdict_the_reviewer_wrote(tmp_path):
    config, state = _config(tmp_path)
    work = _work(tmp_path)
    kwargs = _review_kwargs(tmp_path, work)
    output = Path(kwargs["output"])
    assert not output.exists()
    result = run_executor(
        config,
        state=state,
        runner=_writes(output, f"# Review\nFAIL: P1 defect\n{VERDICT_FAIL}\n"),
        **kwargs,
    )
    assert result["ok"] is True
    assert result["verdict"] == "FAIL"
    review = state.load().reviews[0]
    assert (review.verdict, review.blocking, review.reviewer) == (
        "FAIL",
        True,
        "reviewer.invalid",
    )
    assert review.fingerprint == files_fingerprint(work, ("out.txt",))
    assert review.basis == basis_of(kwargs["intent"], kwargs["spec"], ())


def test_each_review_of_a_card_gets_the_next_round(tmp_path):
    config, state = _config(tmp_path)
    work = _work(tmp_path)
    kwargs = _review_kwargs(tmp_path, work)
    runner = _writes(Path(kwargs["output"]), VERDICT_FAIL + "\n")
    for _ in range(2):
        assert run_executor(config, state=state, runner=runner, **kwargs)["ok"] is True
    assert [item.round for item in state.load().reviews] == [1, 2]


def _review_with_own_state(config, root, tmp_path, work, card, name):
    """One reviewer with its own State, as a second worker process would have."""
    own = State(root)
    real = own.append_review

    def slow(review, body, now):
        time.sleep(0.2)
        real(review, body, now)

    own.append_review = slow
    kwargs = _review_kwargs(tmp_path, work, card=card, output=str(tmp_path / f"{name}.md"))
    runner = _writes(Path(kwargs["output"]), VERDICT_PASS + "\n")
    return run_executor(config, state=own, runner=runner, **kwargs)


def test_reviews_of_one_card_at_the_same_time_get_distinct_rounds(tmp_path):
    config, state = _config(tmp_path)
    work = _work(tmp_path)
    jobs = [
        lambda n=n: _review_with_own_state(config, state.root, tmp_path, work, "c1", f"r{n}")
        for n in range(3)
    ]
    assert all(item["ok"] for item in in_threads(jobs))
    assert sorted(item.round for item in state.load().reviews) == [1, 2, 3]


def test_each_card_counts_its_own_review_rounds(tmp_path):
    config, state = _config(tmp_path)
    work = _work(tmp_path)
    _review_with_own_state(config, state.root, tmp_path, work, "c1", "first")
    _review_with_own_state(config, state.root, tmp_path, work, "c2", "second")
    _review_with_own_state(config, state.root, tmp_path, work, "c1", "third")
    rounds = {(item.card, item.round) for item in state.load().reviews}
    assert rounds == {("c1", 1), ("c2", 1), ("c1", 2)}


def test_review_output_without_one_valid_verdict_line_records_nothing(tmp_path):
    config, state = _config(tmp_path)
    work = _work(tmp_path)
    kwargs = _review_kwargs(tmp_path, work)
    output = Path(kwargs["output"])
    for text in (
        "no verdict here\n",
        f"{VERDICT_PASS}\n{VERDICT_FAIL}\n",
        '{"verdict": "PASS", "blocking": true, "reviewer": "r"}\n',
        '{"verdict": "MAYBE", "blocking": false, "reviewer": "r"}\n',
        '{"verdict": "PASS", "blocking": false}\n',
    ):
        result = run_executor(config, state=state, runner=_writes(output, text), **kwargs)
        assert result["ok"] is False, text
    assert state.load().reviews == ()


def test_review_with_a_nonzero_exit_is_not_a_review(tmp_path):
    config, state = _config(tmp_path)
    work = _work(tmp_path)
    kwargs = _review_kwargs(tmp_path, work)
    result = run_executor(
        config,
        state=state,
        runner=_writes(Path(kwargs["output"]), VERDICT_PASS + "\n", code=1),
        **kwargs,
    )
    assert result["ok"] is False
    assert state.load().reviews == ()


def test_the_record_makes_a_card_the_gate_can_judge(tmp_path):
    config, state = _config(tmp_path)
    work = _work(tmp_path)
    checklist = tmp_path / "check.json"
    checklist.write_text(json.dumps({"items": [{"id": "a", "passes": True}]}), encoding="utf-8")
    run_executor(
        config,
        kind="work",
        card="c1",
        brief="b",
        copy=str(work),
        output="o",
        now=NOW,
        state=state,
        runner=lambda argv, log: 0,
        results=("out.txt",),
        checklist=str(checklist),
    )
    seen = EvidenceBoard(FakeBoard((Card(id="c1", status="running"),)), state).card("c1")
    assert seen.exit_code == 0
    assert seen.checklist_path == str(checklist)
    assert seen.fingerprint == files_fingerprint(work, ("out.txt",))
