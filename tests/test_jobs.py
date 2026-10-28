import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from taskwatch.job_cli import main
from taskwatch.jobs import Jobs, load_job_config
from taskwatch.state import RecordError


def payload(tmp_path: Path, receipt="message:1") -> dict:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    intent = source / "intent.md"
    spec = source / "spec.md"
    intent.write_text("Approved synthetic intent", encoding="utf-8")
    spec.write_text("Synthetic deliverable specification", encoding="utf-8")
    return {
        "receipt_key": receipt,
        "approval_source": "message-1",
        "approval_scope": "local files",
        "prompt": "Preserve this full original prompt",
        "kind": "document",
        "target": str(source),
        "intent": str(intent),
        "spec": str(spec),
        "outputs": ["result.txt"],
        "check_command": ["true"],
        "checklist": "checklist.json",
        "progress": "progress.md",
    }


def test_idempotent_submit_copies_inputs_and_preserves_original(tmp_path):
    jobs = Jobs(tmp_path / "root")
    data = payload(tmp_path)
    first = jobs.submit(data, 10)
    Path(data["intent"]).write_text("Changed after receipt", encoding="utf-8")
    repeated = jobs.submit({"receipt_key": data["receipt_key"]}, 20)
    assert repeated == first
    assert first.prompt == data["prompt"]
    assert Path(first.intent).read_text() == "Approved synthetic intent"
    for name, digest in first.input_hashes.items():
        assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == digest
    assert len(jobs.state.read_records("jobs.jsonl")) == 1


def test_concurrent_receipt_has_one_durable_identity(tmp_path):
    data = payload(tmp_path)
    root = tmp_path / "root"
    with ThreadPoolExecutor(max_workers=4) as pool:
        identifiers = list(pool.map(lambda _: Jobs(root).submit(data).id, range(8)))
    assert len(set(identifiers)) == 1
    assert len(Jobs(root).load()) == 1


def test_corrupt_committed_record_blocks_reads_and_new_submit(tmp_path):
    jobs = Jobs(tmp_path / "root")
    jobs.submit(payload(tmp_path), 10)
    path = jobs.state.dir / "jobs.jsonl"
    data = json.loads(path.read_text())
    data["job"]["stage"] = "deliver_pending"
    path.write_text(json.dumps(data) + "\n")
    with pytest.raises(RecordError, match="corrupt"):
        jobs.load()
    with pytest.raises(RecordError):
        jobs.submit(payload(tmp_path, "message:2"), 20)


def test_torn_tail_preserves_receipt_and_is_repaired_before_append(tmp_path):
    jobs = Jobs(tmp_path / "root")
    first = jobs.submit(payload(tmp_path), 10)
    path = jobs.state.dir / "jobs.jsonl"
    with path.open("ab") as handle:
        handle.write(b'{"version":1,"job":')
    assert jobs.load()[first.id] == first
    jobs.submit(payload(tmp_path, "message:2"), 20)
    assert len(jobs.load()) == 2
    assert len(list(path.parent.glob("jobs.jsonl.torn-*"))) == 1


def test_changed_copied_input_is_not_accepted(tmp_path):
    jobs = Jobs(tmp_path / "root")
    job = jobs.submit(payload(tmp_path), 10)
    Path(job.spec).write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        jobs.verify_inputs(job)


@pytest.mark.parametrize(
    "field,value",
    [
        ("outputs", ["../escape"]),
        ("checklist", "/placeholder/checklist.json"),
        ("check_command", []),
        ("approval_required", "yes"),
        ("expected_wait_seconds", -1),
    ],
)
def test_submit_refuses_unsafe_or_incomplete_contract(tmp_path, field, value):
    jobs = Jobs(tmp_path / "root")
    data = payload(tmp_path)
    data[field] = value
    with pytest.raises(ValueError):
        jobs.submit(data)
    assert jobs.load() == {}


def test_standalone_init_and_submit_without_hermes(tmp_path, capsys):
    root = tmp_path / "root"
    assert main(["--root", str(root), "init"]) == 0
    assert load_job_config(root).slots == 2
    file = tmp_path / "job.json"
    file.write_text(json.dumps(payload(tmp_path)), encoding="utf-8")
    assert main(["--root", str(root), "submit", str(file)]) == 0
    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert Jobs(root).load()[receipt["id"]].stage == "queued"


@pytest.mark.parametrize(
    "change",
    [
        {"approval_required": 0},
        {"input_hashes": {}},
        {"outputs": []},
        {"check_command": [None]},
    ],
)
def test_valid_checksum_does_not_allow_invalid_job_schema(tmp_path, change):
    jobs = Jobs(tmp_path / "root")
    job = jobs.submit(payload(tmp_path), 10)
    data = {**job.to_json(), **change}
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    row = {"version": 1, "job": data, "sha256": hashlib.sha256(encoded).hexdigest()}
    path = jobs.state.dir / "jobs.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    with pytest.raises(RecordError):
        jobs.load()


def test_repeated_approval_wait_preserves_queued_resume_step(tmp_path, capsys):
    root = tmp_path / "root"
    jobs = Jobs(root)
    job = jobs.submit(payload(tmp_path), 10)
    job = replace(job, stage="queued", resume_stage="review", attempt=3)
    jobs.save(job, 11)
    for action in ("wait", "approve", "wait", "approve"):
        assert main(["--root", str(root), action, job.id, "--reason", "message-2"]) == 0
    current = jobs.load()[job.id]
    assert (current.stage, current.resume_stage, current.attempt) == (
        "queued",
        "review",
        3,
    )


def test_approval_records_exact_replacement_scope(tmp_path, capsys):
    root = tmp_path / "root"
    jobs = Jobs(root)
    job = jobs.submit(payload(tmp_path), 10)
    assert main(["--root", str(root), "wait", job.id, "--reason", "Need target write"]) == 0
    assert (
        main(
            [
                "--root",
                str(root),
                "approve",
                job.id,
                "--reason",
                "message-2",
                "--scope",
                "Write only the approved target",
            ]
        )
        == 0
    )
    current = jobs.load()[job.id]
    assert current.approval_source == "message-2"
    assert current.approval_scope == "Write only the approved target"
    assert current.stage == "queued"
