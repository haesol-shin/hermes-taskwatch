"""Read the verdict line and the finding lines of a review output.

A review output holds exactly one line that is a JSON object:
{"verdict": "PASS" or "FAIL", "blocking": true or false, "reviewer": "<name>"}.

Each finding has a heading `### F<n>` and one JSON line
{"finding": "F<n>", "severity": "P0".."P3", "category": "<C-9 category>",
 "rule": "<rule id>", "location": "<where>"}.
The two sets of ids must match. Only these lines are read, never the free text.
"""

import json
import re

SEVERITIES = ("P0", "P1", "P2", "P3")
CATEGORIES = ("intent", "evidence", "behavior", "clarity", "other")
_HEADING = re.compile(r"^###\s+(F\d+)\b", re.MULTILINE)


def _json_lines(body: str, key: str) -> list[dict]:
    found = []
    for line in body.splitlines():
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            obj = json.loads(text)
        except ValueError:
            continue
        if isinstance(obj, dict) and key in obj:
            found.append(obj)
    return found


def _verdict_line(body: str) -> tuple[str, bool, str]:
    found = _json_lines(body, "verdict")
    if len(found) != 1:
        raise ValueError("the review output needs exactly one verdict line")
    obj = found[0]
    verdict, blocking, reviewer = obj.get("verdict"), obj.get("blocking"), obj.get("reviewer")
    if verdict not in {"PASS", "FAIL"} or not isinstance(blocking, bool):
        raise ValueError("the verdict line needs verdict PASS or FAIL and a boolean blocking")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("the verdict line needs a reviewer")
    if verdict == "PASS" and blocking:
        raise ValueError("a PASS cannot carry a blocking finding")
    return verdict, blocking, reviewer.strip()


def _finding_lines(body: str, verdict: str) -> list[dict]:
    """The finding lines, validated against the headings and the verdict."""
    findings = []
    for obj in _json_lines(body, "finding"):
        ident = obj.get("finding")
        if not isinstance(ident, str) or re.fullmatch(r"F\d+", ident) is None:
            raise ValueError("a finding line needs an id like F1")
        if obj.get("severity") not in SEVERITIES or obj.get("category") not in CATEGORIES:
            raise ValueError(f"finding {ident} needs a severity P0 to P3 and a known category")
        for key in ("rule", "location"):
            if not isinstance(obj.get(key), str) or not obj[key].strip():
                raise ValueError(f"finding {ident} needs a nonempty {key}")
        findings.append(
            {
                "finding": ident,
                "severity": obj["severity"],
                "category": obj["category"],
                "rule": obj["rule"].strip(),
                "location": obj["location"].strip(),
            }
        )
    ids = [f["finding"] for f in findings]
    if len(set(ids)) != len(ids):
        raise ValueError("finding ids must be unique")
    if set(_HEADING.findall(body)) != set(ids):
        raise ValueError("the finding headings and the finding lines must name the same ids")
    if verdict == "PASS" and any(f["severity"] in {"P0", "P1"} for f in findings):
        raise ValueError("a PASS cannot carry a P0 or P1 finding")
    return findings
