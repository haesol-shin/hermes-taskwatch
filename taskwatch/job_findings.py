"""Finding records: the worker's status file, the index across jobs, and the repeat trigger.

A finding has a rule id, so two findings are of the same kind when `rule` and the job `target`
match; `other` is never a kind. Nothing here reads free text or calls a model.
"""

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from .jobs import JobState
from .state import State

FINDINGS_FILE = "findings.json"
NOTE_FILE = "root-cause.md"
INDEX = "findings.jsonl"
WINDOW = 10
NOTE_LABELS = ("Cause:", "Missed by:", "Prevention:")


def finding_id(review: Mapping, finding: Mapping) -> str:
    return f"r{review['round']}.{finding['finding']}"


def _entries(folder: Path) -> dict[str, Mapping]:
    try:
        raw = json.loads((folder / FINDINGS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, list):
        return {}
    return {e["id"]: e for e in raw if isinstance(e, dict) and isinstance(e.get("id"), str)}


def status_lines(job: JobState, folder: Path) -> list[str]:
    """The worker's recorded status of the findings of the last review, for the next reviewer."""
    review = job.reviews[-1] if job.reviews else {}
    entries = _entries(folder)
    lines = []
    for finding in review.get("findings", []):
        entry = entries.get(finding_id(review, finding))
        if entry is None:
            continue
        reason = f": {entry['reason']}" if entry.get("status") == "not_fixed" else ""
        lines.append(
            f"- {finding_id(review, finding)} ({finding['severity']}) {entry.get('status')}{reason}"
        )
    return lines


def worker_problems(
    folder: Path, review: Mapping, fingerprint: str, texts: Mapping[str, Mapping]
) -> list[str]:
    """Check problems of the worker's record for the findings of the last review."""
    findings = review.get("findings", [])
    if not findings:
        return []
    if not (folder / FINDINGS_FILE).is_file():
        return [f"{FINDINGS_FILE} is missing: record each finding of the last review"]
    entries = _entries(folder)
    reviewed = review.get("texts", {})
    changed = fingerprint != review.get("fingerprint") or any(
        texts.get(unit, {}).get("fingerprint") != state.get("fingerprint")
        for unit, state in reviewed.items()
    )
    problems = []
    for finding in findings:
        key = finding_id(review, finding)
        entry = entries.get(key)
        status = entry.get("status") if entry else None
        if entry is None:
            problems.append(f"{FINDINGS_FILE} has no entry for {key}")
        elif status == "fixed" and not changed:
            problems.append(f"{key} is fixed, but neither the code nor the public text changed")
        elif status == "not_fixed":
            reason = entry.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                problems.append(f"{key} is not_fixed and needs a reason")
        elif status not in {"fixed", "not_fixed"}:
            problems.append(f"{key} needs status fixed or not_fixed")
    return problems


def repeated_rules(
    job: JobState, earlier: Sequence[Mapping], latest: Mapping, rows: Sequence[Mapping]
) -> list[str]:
    """Rules of the latest review that an earlier round or one of the last jobs also broke."""
    seen = {f["rule"] for review in earlier for f in review.get("findings", [])}
    order: list[str] = []
    by_job: dict[str, set[str]] = {}
    for row in rows:
        if row.get("target") == job.target and row.get("job") != job.id:
            if row["job"] not in by_job:
                order.append(row["job"])
            by_job.setdefault(row["job"], set()).add(row["rule"])
    for other in order[-WINDOW:]:
        seen |= by_job[other]
    rules = {f["rule"] for f in latest.get("findings", []) if f["rule"] != "other"}
    return sorted(rules & seen)


def notes(folder: Path) -> dict[str, dict[str, str]]:
    """`## <rule>` sections of the root-cause note, each with its labeled lines."""
    try:
        text = (folder / NOTE_FILE).read_text(encoding="utf-8")
    except OSError:
        return {}
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in text.splitlines():
        heading = re.fullmatch(r"##\s+(\S.*?)\s*", line)
        if heading:
            current = sections.setdefault(heading.group(1), {})
            continue
        for label in NOTE_LABELS:
            if current is not None and line.startswith(label):
                current[label] = line[len(label) :].strip()
    return sections


def note_problems(folder: Path, rules: Sequence[str]) -> list[str]:
    sections = notes(folder)
    problems = []
    for rule in rules:
        section = sections.get(rule)
        if section is None:
            problems.append(f"{NOTE_FILE} needs a section `## {rule}`: the rule repeated")
            continue
        for label in NOTE_LABELS:
            if not section.get(label):
                problems.append(f"{NOTE_FILE} section `## {rule}` needs a `{label}` line")
    return problems


def named_preventions(folder: Path, rules: Sequence[str]) -> dict[str, str]:
    """The prevention a note names for each rule; `accepted: <reason>` names none."""
    found = {}
    for rule in rules:
        text = notes(folder).get(rule, {}).get("Prevention:", "")
        if text and not text.lower().startswith("accepted:"):
            found[rule] = text
    return found


# --- index ---------------------------------------------------------------------------------


def sync_index(state: State, job: JobState, folder: Path, now: float) -> None:
    """Append one line per finding whose status is new; the last line of a finding wins."""
    last: dict[tuple[str, str], str] = {}
    for row in state.read_records(INDEX):
        last[(row["job"], row["finding"])] = row["status"]
    entries = _entries(folder)
    stamp = datetime.fromtimestamp(now, timezone.utc)
    for review in job.reviews:
        for finding in review.get("findings", []):
            key = finding_id(review, finding)
            entry = entries.get(key)
            status = entry["status"] if entry and entry.get("status") else "open"
            if last.get((job.id, key)) == status:
                continue
            state.append_record(
                INDEX,
                {
                    "job": job.id,
                    "target": job.target,
                    "round": review["round"],
                    "finding": key,
                    "rule": finding["rule"],
                    "status": status,
                },
                stamp,
            )
            last[(job.id, key)] = status
