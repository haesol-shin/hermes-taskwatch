---
name: taskwatch-worker
description: Run the configured executor in the foreground, then hand the card to Kanban.
---

# Taskwatch worker

This file is an example. Installing it in a profile is a separate step.

You are a Kanban worker. You do not merge, and you do not change another card.

1. Run `examples/worker/run_executor.py` with the Hermes `terminal` tool in the foreground, using the Python that can import Hermes. Wait for the process to exit. Do not background it, and do not continue because a completion notice arrived. Give it `--kind work`, the brief, the work copy that will hold the result (`--copy`), the executor's output path (`--output`), every required result file under the copy (`--result`, repeatable), and the checklist (`--checklist`). The card id defaults to this task.
2. Read the one JSON line it prints. It has already written the run record the automatic gate reads. Do not write the exit code, outputs, or fingerprint on the card yourself.
3. If `ok` is false, call `kanban_block` with the `block` reason. Do not call `kanban_complete` or `kanban_request_review`.
4. If `ok` is true and this card is not a gate, call `kanban_complete`.
5. If `ok` is true and this card is a gate, call `kanban_request_review`.
6. The automatic gate may still block that call. If it does, stop and report the block message. Do not retry by editing the card status yourself.

A card whose parent gate was approved gets its own work copy made from the approved snapshot, at the `--copy` path. The script does that: the executor, the checks, and the record all use that copy, and the snapshot itself is never changed. A copy already at that path is moved aside, not deleted. If a parent gate has no approved snapshot, the script prints `ok` false and does not run the executor: report the block. Do not point the executor at the parent's live work copy.

A review run uses `--kind review`, the reviewed `--copy`, the same `--result` files, and for a Git copy `--git --sha <reviewed commit>`. The reviewer's output file must hold exactly one line like `{"verdict": "PASS", "blocking": false, "reviewer": "<name>"}`. The verdict is recorded from that line, not from the exit code. `ok` true with `"verdict": "FAIL"` means the review ran and failed: report it, and do not call `kanban_complete` for the gate.
