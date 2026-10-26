"""Count missed findings, false positives, severity mismatches, and verdict agreement.

A person maps each reviewer finding to at most one expected finding.
This script does not decide whether a cause matches. It only counts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

VERDICTS = frozenset({"correct", "incorrect"})
SEVERITIES = frozenset({"P0", "P1", "P2", "P3"})
ANSWER_FIELDS = ("id", "location", "severity", "cause")
CONDITIONS = frozenset({"severity-p2"})


class ScoreError(ValueError):
    """A mapping or answer file cannot be counted."""


def score_case(answer: dict, mapping: dict) -> dict:
    expected = validate_answer(answer)
    validate_mapping(mapping, expected)
    matched = {item["expected"] for item in mapping["matches"]}
    missed = [item for item in expected if item not in matched]
    mismatches = [
        item for item in mapping["matches"] if item["severity"] != expected[item["expected"]]
    ]
    scored = {
        "missed": len(missed),
        "false_positives": len(mapping["unmatched"]),
        "severity_mismatches": len(mismatches),
        "verdict_agreement": mapping["verdict"] == answer["verdict"],
    }
    condition = answer.get("condition")
    if condition is not None:
        scored["condition"] = {
            "name": condition,
            "passed": _severity_p2_passed(expected, mapping),
        }
    return scored


def _severity_p2_passed(expected: dict[str, str], mapping: dict) -> bool:
    # The only expected finding must be matched at exactly P2, with the verdict correct.
    (expected_id,) = expected
    matches = [item for item in mapping["matches"] if item["expected"] == expected_id]
    matched_at = [item["severity"] for item in matches]
    return matched_at == ["P2"] and mapping["verdict"] == "correct"


def validate_answer(answer: dict) -> dict[str, str]:
    """Return the expected finding ids with their severities."""
    verdict = answer.get("verdict")
    if verdict not in VERDICTS:
        raise ScoreError(f"answer verdict must be correct or incorrect, got {verdict!r}")
    findings = answer.get("findings")
    if not isinstance(findings, list):
        raise ScoreError("answer findings must be a list")
    expected: dict[str, str] = {}
    for index, finding in enumerate(findings):
        finding_id = _finding_id(finding, index)
        if finding_id in expected:
            raise ScoreError("answer finding ids must be unique")
        expected[finding_id] = finding["severity"]
    _validate_condition(answer, expected)
    return expected


def _validate_condition(answer: dict, expected: dict[str, str]) -> None:
    condition = answer.get("condition")
    if condition is None:
        return
    if condition not in CONDITIONS:
        raise ScoreError(f"unknown answer condition {condition!r}")
    if list(expected.values()) != ["P2"] or answer["verdict"] != "correct":
        raise ScoreError("severity-p2 needs one P2 finding and the verdict correct")


def validate_mapping(mapping: dict, expected: dict[str, str]) -> None:
    if mapping.get("verdict") not in VERDICTS:
        raise ScoreError("mapping verdict must be correct or incorrect")
    matches = mapping.get("matches")
    unmatched = mapping.get("unmatched")
    if not isinstance(matches, list) or not isinstance(unmatched, list):
        raise ScoreError("mapping matches and unmatched must be lists")
    seen_expected: set[str] = set()
    seen_findings: set[str] = set()
    for item in matches:
        finding_id = _require_id(item, "finding")
        expected_id = _require_id(item, "expected")
        _require_reason(item)
        if item.get("severity") not in SEVERITIES:
            raise ScoreError("each match needs the reviewer's severity: P0, P1, P2, or P3")
        if expected_id not in expected:
            raise ScoreError(f"match refers to unknown expected finding {expected_id}")
        if expected_id in seen_expected or finding_id in seen_findings:
            raise ScoreError("a match must be one reviewer finding to one expected finding")
        seen_expected.add(expected_id)
        seen_findings.add(finding_id)
    for item in unmatched:
        finding_id = _require_id(item, "finding")
        _require_reason(item)
        if finding_id in seen_findings:
            raise ScoreError(f"finding {finding_id} is both matched and unmatched")
        seen_findings.add(finding_id)


def score_dirs(answers: Path, mappings: Path) -> dict:
    if not answers.is_dir():
        raise ScoreError(f"missing answers directory: {answers}")
    if not mappings.is_dir():
        raise ScoreError(f"missing mappings directory: {mappings}")
    cases: dict[str, dict] = {}
    for answer_path in sorted(answers.glob("*.json")):
        name = answer_path.stem
        mapping_path = mappings / f"{name}.json"
        if not mapping_path.is_file():
            raise ScoreError(f"missing mapping for {name}")
        answer = _load_json(answer_path)
        mapping = _load_json(mapping_path)
        if mapping.get("case") != name:
            raise ScoreError(f"{name}: mapping case does not match the file name")
        cases[name] = score_case(answer, mapping)
    extra = sorted(path.stem for path in mappings.glob("*.json") if path.stem not in cases)
    if extra:
        raise ScoreError(f"mapping without an answer: {', '.join(extra)}")
    if not cases:
        raise ScoreError("no cases to score")
    agreements = sum(1 for item in cases.values() if item["verdict_agreement"])
    failed_conditions = sum(
        1 for item in cases.values() if "condition" in item and not item["condition"]["passed"]
    )
    return {
        "cases": cases,
        "totals": {
            "missed": sum(item["missed"] for item in cases.values()),
            "false_positives": sum(item["false_positives"] for item in cases.values()),
            "severity_mismatches": sum(item["severity_mismatches"] for item in cases.values()),
            "failed_conditions": failed_conditions,
            "verdict_agreements": agreements,
            "cases": len(cases),
        },
    }


def _finding_id(finding: object, index: int) -> str:
    if not isinstance(finding, dict):
        raise ScoreError(f"finding {index} must be an object")
    for field in ANSWER_FIELDS:
        value = finding.get(field)
        if not isinstance(value, str) or value.strip() == "":
            raise ScoreError(f"finding {index} needs a non-empty {field}")
    if finding["severity"] not in SEVERITIES:
        raise ScoreError(f"finding {index} severity must be P0, P1, P2, or P3")
    if "\n" in finding["cause"]:
        raise ScoreError(f"finding {index} cause must be one line")
    return finding["id"]


def _require_id(item: object, field: str) -> str:
    if not isinstance(item, dict):
        raise ScoreError(f"mapping {field} entry must be an object")
    value = item.get(field)
    if not isinstance(value, str) or value.strip() == "":
        raise ScoreError(f"mapping entry needs a non-empty {field}")
    return value


def _require_reason(item: dict) -> None:
    reason = item.get("reason")
    if not isinstance(reason, str) or reason.strip() == "" or "\n" in reason:
        raise ScoreError("each match and unmatched finding needs a one-line reason")


def _load_json(path: Path) -> dict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScoreError(f"{path.name}: invalid JSON") from exc
    if not isinstance(loaded, dict):
        raise ScoreError(f"{path.name}: JSON must be an object")
    return loaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Count a mapped review run.")
    parser.add_argument("--answers", type=Path, required=True)
    parser.add_argument("--mappings", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = score_dirs(args.answers, args.mappings)
    except ScoreError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
