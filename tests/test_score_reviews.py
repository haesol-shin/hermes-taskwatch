import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "score_reviews.py"

DEFECT_ANSWER = {
    "verdict": "incorrect",
    "findings": [
        {
            "id": "F1",
            "location": "src/example.py:4-6",
            "severity": "P1",
            "cause": "The requested click never happens.",
        }
    ],
}

CLEAN_ANSWER = {"verdict": "correct", "findings": []}


def load_scorer():
    spec = importlib.util.spec_from_file_location("score_reviews", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


SCORER = load_scorer()


def mapping(verdict, matches, unmatched):
    return {
        "case": "sample",
        "verdict": verdict,
        "matches": matches,
        "unmatched": unmatched,
    }


def match(finding, expected, reason, severity="P1"):
    return {"finding": finding, "expected": expected, "reason": reason, "severity": severity}


def unmatched(finding, reason):
    return {"finding": finding, "reason": reason}


def test_defect_correct_review_is_zero_zero():
    scored = SCORER.score_case(
        DEFECT_ANSWER,
        mapping(
            "incorrect",
            [match("1", "F1", "Same lines and the same missing click.")],
            [],
        ),
    )
    assert scored["missed"] == 0
    assert scored["false_positives"] == 0
    assert scored["verdict_agreement"] is True


def test_right_place_wrong_cause_is_one_one():
    scored = SCORER.score_case(
        DEFECT_ANSWER,
        mapping(
            "incorrect",
            [],
            [unmatched("1", "Same lines, but the cause is a style nit.")],
        ),
    )
    assert scored["missed"] == 1
    assert scored["false_positives"] == 1


def test_extra_wrong_finding_is_zero_one():
    scored = SCORER.score_case(
        DEFECT_ANSWER,
        mapping(
            "incorrect",
            [match("1", "F1", "Same lines and the same missing click.")],
            [unmatched("2", "Claims a failure the change does not have.")],
        ),
    )
    assert scored["missed"] == 0
    assert scored["false_positives"] == 1


def test_clean_with_no_findings_is_zero_zero():
    scored = SCORER.score_case(CLEAN_ANSWER, mapping("correct", [], []))
    assert scored["missed"] == 0
    assert scored["false_positives"] == 0
    assert scored["verdict_agreement"] is True


def test_clean_with_a_wrong_finding_is_zero_one():
    scored = SCORER.score_case(
        CLEAN_ANSWER,
        mapping("incorrect", [], [unmatched("1", "Reports a behavior change that is not there.")]),
    )
    assert scored["missed"] == 0
    assert scored["false_positives"] == 1
    assert scored["verdict_agreement"] is False


def test_clean_match_is_rejected():
    bad = mapping("incorrect", [match("1", "F1", "No expected finding exists.")], [])
    try:
        SCORER.score_case(CLEAN_ANSWER, bad)
    except SCORER.ScoreError as exc:
        assert "unknown expected finding" in str(exc)
    else:
        raise AssertionError("a clean case must not accept a match")


def test_cli_counts_a_mapped_run(tmp_path: Path, capsys):
    answers = tmp_path / "answers"
    mappings = tmp_path / "mappings"
    answers.mkdir()
    mappings.mkdir()
    (answers / "sample.json").write_text(json.dumps(DEFECT_ANSWER), encoding="utf-8")
    mapped = mapping(
        "incorrect",
        [match("1", "F1", "Same lines and the same missing click.")],
        [],
    )
    mapped["case"] = "sample"
    (mappings / "sample.json").write_text(json.dumps(mapped), encoding="utf-8")
    assert SCORER.main(["--answers", str(answers), "--mappings", str(mappings)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["cases"]["sample"]["missed"] == 0
    assert payload["cases"]["sample"]["false_positives"] == 0
    assert payload["totals"]["verdict_agreements"] == 1


def test_missing_answers_dir_fails(tmp_path: Path, capsys):
    mappings = tmp_path / "mappings"
    mappings.mkdir()
    missing = tmp_path / "no-answers"
    code = SCORER.main(["--answers", str(missing), "--mappings", str(mappings)])
    captured = capsys.readouterr()
    assert code != 0
    assert captured.out == ""
    assert f"missing answers directory: {missing}" in captured.err


def test_missing_mappings_dir_fails(tmp_path: Path, capsys):
    answers = tmp_path / "answers"
    answers.mkdir()
    (answers / "sample.json").write_text(json.dumps(DEFECT_ANSWER), encoding="utf-8")
    missing = tmp_path / "no-mappings"
    code = SCORER.main(["--answers", str(answers), "--mappings", str(missing)])
    captured = capsys.readouterr()
    assert code != 0
    assert captured.out == ""
    assert f"missing mappings directory: {missing}" in captured.err


def test_zero_cases_fail(tmp_path: Path, capsys):
    answers = tmp_path / "answers"
    mappings = tmp_path / "mappings"
    answers.mkdir()
    mappings.mkdir()
    code = SCORER.main(["--answers", str(answers), "--mappings", str(mappings)])
    captured = capsys.readouterr()
    assert code != 0
    assert captured.out == ""
    assert "no cases to score" in captured.err


def test_cli_totals_count_misses_and_false_positives(tmp_path: Path, capsys):
    answers = tmp_path / "answers"
    mappings = tmp_path / "mappings"
    answers.mkdir()
    mappings.mkdir()

    def write_case(name, answer, verdict, matches, unmatched):
        (answers / f"{name}.json").write_text(json.dumps(answer), encoding="utf-8")
        mapped = mapping(verdict, matches, unmatched)
        mapped["case"] = name
        (mappings / f"{name}.json").write_text(json.dumps(mapped), encoding="utf-8")

    write_case(
        "missed-and-extra",
        DEFECT_ANSWER,
        "incorrect",
        [],
        [unmatched("1", "Names the file but not the missing click.")],
    )
    write_case(
        "clean-extra",
        CLEAN_ANSWER,
        "incorrect",
        [],
        [unmatched("9", "Reports a change that is not in the diff.")],
    )
    write_case(
        "matched-wrong-verdict",
        DEFECT_ANSWER,
        "correct",
        [match("1", "F1", "Same lines and the same missing click.")],
        [],
    )
    assert SCORER.main(["--answers", str(answers), "--mappings", str(mappings)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["missed"] == 1
    assert payload["totals"]["false_positives"] == 2
    assert payload["totals"]["verdict_agreements"] == 1
    assert payload["totals"]["cases"] == 3


P2_ANSWER = {
    "condition": "severity-p2",
    "verdict": "correct",
    "findings": [
        {
            "id": "F1",
            "location": "docs/usage.md:5",
            "severity": "P2",
            "cause": "The document still states the old default.",
        }
    ],
}


def test_match_at_the_expected_severity_has_no_mismatch():
    scored = SCORER.score_case(
        DEFECT_ANSWER,
        mapping("incorrect", [match("1", "F1", "Same cause.", "P1")], []),
    )
    assert scored["severity_mismatches"] == 0


def test_match_at_another_severity_is_one_mismatch_and_not_a_miss():
    scored = SCORER.score_case(
        DEFECT_ANSWER,
        mapping("incorrect", [match("1", "F1", "Same cause.", "P0")], []),
    )
    assert scored["severity_mismatches"] == 1
    assert scored["missed"] == 0
    assert scored["false_positives"] == 0


def test_unmatched_finding_is_not_a_severity_mismatch():
    scored = SCORER.score_case(
        DEFECT_ANSWER,
        mapping("incorrect", [], [unmatched("1", "A different cause.")]),
    )
    assert scored["severity_mismatches"] == 0


@pytest.mark.parametrize("severity", [None, "P4", "high", ""])
def test_match_needs_the_reviewer_severity(severity):
    bad = match("1", "F1", "Same cause.")
    if severity is None:
        del bad["severity"]
    else:
        bad["severity"] = severity
    with pytest.raises(SCORER.ScoreError, match="severity"):
        SCORER.score_case(DEFECT_ANSWER, mapping("incorrect", [bad], []))


def test_severity_p2_passes_at_p2_with_verdict_correct():
    scored = SCORER.score_case(
        P2_ANSWER,
        mapping("correct", [match("1", "F1", "Same stale line.", "P2")], []),
    )
    assert scored["condition"] == {"name": "severity-p2", "passed": True}
    assert scored["severity_mismatches"] == 0


@pytest.mark.parametrize(
    ("verdict", "severity"),
    [("correct", "P1"), ("correct", "P3"), ("incorrect", "P0"), ("incorrect", "P2")],
)
def test_severity_p2_fails_off_p2_or_without_verdict_correct(verdict, severity):
    scored = SCORER.score_case(
        P2_ANSWER,
        mapping(verdict, [match("1", "F1", "Same stale line.", severity)], []),
    )
    assert scored["condition"]["passed"] is False
    assert scored["missed"] == 0


def test_severity_p2_fails_when_the_finding_is_missed():
    scored = SCORER.score_case(P2_ANSWER, mapping("correct", [], []))
    assert scored["condition"]["passed"] is False
    assert scored["missed"] == 1


def test_case_without_a_condition_reports_none():
    scored = SCORER.score_case(DEFECT_ANSWER, mapping("incorrect", [], []))
    assert "condition" not in scored


@pytest.mark.parametrize(
    "answer",
    [
        {**P2_ANSWER, "condition": "severity-p1"},
        {**P2_ANSWER, "verdict": "incorrect"},
        {**P2_ANSWER, "findings": []},
        {
            **P2_ANSWER,
            "findings": [{**P2_ANSWER["findings"][0], "severity": "P1"}],
        },
    ],
)
def test_severity_p2_answer_must_be_one_p2_finding_with_verdict_correct(answer):
    with pytest.raises(SCORER.ScoreError):
        SCORER.validate_answer(answer)


def test_cli_totals_count_severity_mismatches_and_failed_conditions(tmp_path: Path, capsys):
    answers = tmp_path / "answers"
    mappings = tmp_path / "mappings"
    answers.mkdir()
    mappings.mkdir()

    def write_case(name, answer, verdict, matches):
        (answers / f"{name}.json").write_text(json.dumps(answer), encoding="utf-8")
        mapped = mapping(verdict, matches, [])
        mapped["case"] = name
        (mappings / f"{name}.json").write_text(json.dumps(mapped), encoding="utf-8")

    write_case("louder", DEFECT_ANSWER, "incorrect", [match("1", "F1", "Same cause.", "P0")])
    write_case("exact", DEFECT_ANSWER, "incorrect", [match("1", "F1", "Same cause.", "P1")])
    write_case("stale-loud", P2_ANSWER, "incorrect", [match("1", "F1", "Same line.", "P1")])
    write_case("stale-exact", P2_ANSWER, "correct", [match("1", "F1", "Same line.", "P2")])
    assert SCORER.main(["--answers", str(answers), "--mappings", str(mappings)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["severity_mismatches"] == 2
    assert payload["totals"]["failed_conditions"] == 1
    assert payload["cases"]["stale-loud"]["condition"]["passed"] is False
    assert payload["cases"]["stale-exact"]["condition"]["passed"] is True
