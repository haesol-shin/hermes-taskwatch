import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / "evals"
CASES = EVALS / "cases"
ANSWERS = EVALS / "answers"
REVIEW_DOCS = ROOT / "docs" / "review"
LOCATION = re.compile(r"^([^:]+):(\d+)(?:-(\d+))?$")
RULE_ID = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,3}-\d+)(?![A-Za-z0-9])")
DEFINED_RULE = re.compile(r"\*\*([A-Z]{1,3}-\d+)\.\*\*")
HANGUL = re.compile(r"[\uac00-\ud7a3]")
TEXT_SUFFIXES = {".md", ".py", ".json", ".patch"}
FORBIDDEN = (
    "/home/",
    "youtube",
    "YouTube",
    "kanban_",
)

SCRIPT = ROOT / "scripts" / "score_reviews.py"

CASE_NAMES = [
    "dedupe-helper-intent",
    "dedupe-message",
    "dispatch-outside-diff",
    "forced-click",
    "intent-mismatch",
    "lock-wait",
    "repo-rule",
    "severity-p2",
    "stall-reset",
    "subfolder-adopt",
    "truncated-body",
    "unsupported-claim",
    "weak-test",
]
CLEAN_NAMES = {"dedupe-helper-intent", "dedupe-message", "truncated-body"}


def load_scorer():
    spec = importlib.util.spec_from_file_location("score_reviews", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


SCORER = load_scorer()


def load_answers() -> dict[str, dict]:
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8")) for path in ANSWERS.glob("*.json")
    }


def input_files(name: str) -> list[Path]:
    return sorted(path for path in (CASES / name / "input").rglob("*") if path.is_file())


def test_thirteen_cases_keep_answers_outside_input():
    names = sorted(path.name for path in CASES.iterdir() if path.is_dir())
    assert names == CASE_NAMES
    assert sorted(path.stem for path in ANSWERS.glob("*.json")) == names
    for name in names:
        case_input = CASES / name / "input"
        assert case_input.is_dir()
        assert not (case_input / "answer.json").exists()
        assert not (case_input / f"{name}.json").exists()
        assert (case_input / "spec.md").is_file()
        assert (case_input / "diff.patch").is_file()


def test_ten_defect_cases_and_three_clean_cases():
    answers = load_answers()
    clean = {name for name, answer in answers.items() if not answer["findings"]}
    assert clean == CLEAN_NAMES
    for name in clean:
        assert answers[name]["verdict"] == "correct"
    for name in set(answers) - clean:
        assert answers[name]["findings"]


def test_every_answer_has_a_unique_purpose_and_real_rule_ids():
    defined: set[str] = set()
    for path in REVIEW_DOCS.glob("*.md"):
        defined.update(DEFINED_RULE.findall(path.read_text(encoding="utf-8")))
    purposes: list[str] = []
    for name, answer in load_answers().items():
        purpose = answer.get("purpose")
        assert isinstance(purpose, str) and purpose.strip(), name
        purposes.append(purpose)
        rules = answer.get("rules")
        assert isinstance(rules, list) and rules, name
        assert set(rules) <= defined, (name, set(rules) - defined)
    assert len(purposes) == len(set(purposes))


def test_answer_only_fields_are_where_the_case_needs_them():
    answers = load_answers()
    assert answers["weak-test"]["broken_variant"].strip()
    assert answers["severity-p2"]["condition"] == "severity-p2"
    assert answers["truncated-body"]["version_note"].strip()
    assert {name for name, answer in answers.items() if "condition" in answer} == {"severity-p2"}
    assert {name for name, answer in answers.items() if "broken_variant" in answer} == {"weak-test"}


def test_nothing_in_input_names_the_answer():
    answers = load_answers()
    forbidden = [*answers, *(a["purpose"] for a in answers.values())]
    forbidden.extend(a["broken_variant"] for a in answers.values() if "broken_variant" in a)
    for name in answers:
        for path in input_files(name):
            relative = str(path.relative_to(CASES / name / "input"))
            text = path.read_text(encoding="utf-8")
            assert RULE_ID.search(text) is None, path
            for token in forbidden:
                assert token not in text, (path, token)
                assert token not in relative, (path, token)


def test_intent_files_are_only_on_the_intent_cases():
    with_intent = {"intent-mismatch", "dedupe-helper-intent"}
    for case in CASES.iterdir():
        has_intent = (case / "input" / "intent.md").is_file()
        assert has_intent is (case.name in with_intent)


def test_repo_rule_case_ships_the_agents_file_and_no_other_case_does():
    for case in CASES.iterdir():
        has_rules = (case / "input" / "AGENTS.md").is_file()
        assert has_rules is (case.name == "repo-rule")


def test_answer_locations_exist_in_the_case_input():
    for name, answer in load_answers().items():
        SCORER.validate_answer(answer)
        for finding in answer["findings"]:
            matched = LOCATION.fullmatch(finding["location"])
            assert matched, finding["location"]
            source = CASES / name / "input" / matched.group(1)
            assert source.is_file()
            line_count = len(source.read_text(encoding="utf-8").splitlines())
            start = int(matched.group(2))
            end = int(matched.group(3) or start)
            assert 1 <= start <= end <= line_count


def test_mapping_example_has_the_counted_fields():
    example = json.loads((EVALS / "mapping.example.json").read_text(encoding="utf-8"))
    assert example["case"] == "example-case"
    assert {"finding", "expected", "severity", "reason"} <= set(example["matches"][0])
    assert {"finding", "reason"} <= set(example["unmatched"][0])


def test_new_review_files_have_no_personal_values():
    roots = [ROOT / "docs" / "review", EVALS, ROOT / "scripts"]
    for root in roots:
        for path in root.rglob("*"):
            if path.suffix not in TEXT_SUFFIXES or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            assert HANGUL.search(text) is None, path
            for token in FORBIDDEN:
                assert token not in text, (path, token)
