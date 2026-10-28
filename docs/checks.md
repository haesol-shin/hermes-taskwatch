# Checks

Every check is plain code and never calls a model. Each one is in the table with the failure it blocks and the condition under which it can be removed. When Hermes itself enforces the same thing, delete the check. The reasoning is in [principle 7](principles.md).

A check returns causes and never changes a card. A cause has an id, a one-line summary, one next action, and a mark saying whether it is certain or uncertain. Gate checks block a tool call. Watch checks only warn.

A check that cannot read its input is not a pass. Checks that read records, the board, or the config return an uncertain cause marked as unreadable. When a result, intent, spec, or guideline file cannot be read, the fingerprint or basis has no value, so no PASS matches and the cause is the usual missing PASS. A gate blocks on both. When a record cannot be read the scan reports the unreadable cause as a warning. When the config cannot be read the gate blocks every `kanban_complete` and `kanban_request_review` call until it is fixed. The dispatch-tick watch stays quiet when the config file is missing and returns the error when it exists but cannot be read. See [scan read failures](configuration.md#scan-read-failures) for scan notification and exit behavior, and [Known limitations](../README.md#known-limitations).

## Gate checks

The gate runs inside the Hermes process that makes the call, in every profile listed in the config. See [Not covered](#not-covered) for its scope.

| Tool call | Checks that run |
| --- | --- |
| `kanban_request_review` | `worker_finish` |
| `kanban_complete` on a work card | `worker_finish` |
| `kanban_complete` on a gate card | `worker_finish`, `pass_review`, `clean_result` |
| `hermes taskwatch check <card>` | the row above when the card is a gate in review, otherwise `worker_finish` only |
| `hermes taskwatch check <card> --apply` | all four |

The card comes from the call's `task_id`, then from `HERMES_KANBAN_TASK`. The hook's own `task_id` is an execution id and is not used. The board is the database the call really opens, resolved through Hermes from the arguments and any worker database pin. A call from a profile outside the list, or on another board's database, is not checked. A call from a listed profile whose target cannot be resolved is blocked, and so is a call when the config, the board, or the records cannot be read.

The exit code, required outputs, fingerprint, basis, and uncommitted state of a card come from the run record and from the result as it is now. They never come from fields the card carries. A card with no run record has none of them, so it cannot pass. See [Approval records](configuration.md#approval-records) for the separate approval step.

| Check | Blocks | Can be removed when |
| --- | --- | --- |
| `worker_finish` | Completing without a zero exit, required outputs, and a passing checklist. | Hermes refuses those calls on the same evidence. |
| `pass_review` | Approving a result with no PASS for the current fingerprint and basis. | Hermes stores the reviewed fingerprint and refuses a mismatch. |
| `clean_result` | Approving or applying a work copy that changed after the PASS. | Hermes rejects a dirty work copy on gate pass and apply. |
| `apply_match` | Applying a result other than the approved fingerprint. | The apply command compares the approved fingerprint itself. |

The checklist is a JSON file that is either a list of items or an object with an `items` list. Each item has a boolean `passes`. A missing file or a failing item blocks. A file that cannot be read as that shape, including an empty list, also blocks, and the cause is marked uncertain.

### Review records

A review run writes one record per round. The reviewer's output file must hold exactly one line that is a JSON object like `{"verdict": "PASS", "blocking": false, "reviewer": "<name>"}`. `verdict` is `PASS` or `FAIL`. A `PASS` cannot be blocking. The record is read from that line, not from the exit code.

The record names the fingerprint of the result and the basis it was reviewed against. For a Git result the fingerprint is the commit SHA, and a work copy with uncommitted changes is refused. For any other result it is the hash of the sorted list of path and SHA-256 pairs of the named result files, so an addition, a deletion, or a rename changes it. The basis is the same kind of hash over the intent file, the spec file, and the review guideline files.

`taskwatch_reject` does not support a review card, because Hermes has no public atomic reopen. It refuses and changes nothing.

Only the latest round for the same fingerprint and basis counts. An earlier PASS cannot outvote a later FAIL, and a PASS that carries a blocking finding is not a pass. A changed result, intent, or spec makes every earlier PASS stop counting.

## Watch checks

The scan runs these on every pass. A scan of a target that does not match the config, meaning a different root, board, or a profile outside the list, runs none of them and writes a single `target` warning. A warning card is taskwatch's own output and is never checked as work.

| Check | Blocks | Can be removed when |
| --- | --- | --- |
| `gate_action` | A gate card with no real action after its latest review request. | Hermes records that action gap itself. |
| `reject_followup` | A started reject that never reaches a new review request. | Hermes tracks reject completion itself. |
| `next_not_started` | A recorded next card that does not start. | Hermes starts that next card itself. |
| `intermediate_failure` | An intermediate card left in a failure status. | Hermes wakes the main agent for those statuses. |
| `unstarted_after_parents` | A card whose parents are done but which never starts. | Hermes promotes a ready child and alerts. |
| `start_without_card` | A start approval that never becomes a linked card. | Card creation is transactional with the approval record. |
| `gate_subscription` | A gate card with no main subscription. | Hermes refuses to drop that subscription. |
| `checklist_done` | A done card whose checklist has a failing item. | Hermes refuses done unless the checklist passes. |
| `reject_cap` | Another automatic reject after the cap. | Hermes enforces the reject cap. |
| `stalled` | A running card whose progress and outputs are unchanged. | Hermes reports that card-level stall. |
| `unchecked_result` | A done result with no PASS for its fingerprint and basis. | The gate blocks done without that PASS. |
| `applied_differs` | An applied fingerprint that is not in the approval record. | Apply refuses a fingerprint mismatch before it writes. |

A watch check reports a card, not a process. A dead process, a claim that outlives its time limit, and a stalled child run belong to Hermes and are not checked here.

## How a warning is delivered

A new cause writes a warning card, subscribes the main destination to it, and completes it with the cause as the summary. The completion is what wakes the main agent. If the subscription cannot be confirmed, the card is archived and nothing is recorded, so the next scan tries again.

The same cause is written again only after the re-warn wait, which grows by `rewarn_backoff` each time, and stops at `rewarn_cap`. See [configuration](configuration.md#times).

The person is told when any of three conditions holds: a warning has no received reply after `user_notify_unreceived_seconds`, its subscription was removed, or the cap was reached. A received reply does not prevent the latter two conditions. A line is recorded only after it was delivered. A failed delivery is tried again on the next scan.

## Not covered

The gate covers agent tool calls only. A person running `hermes kanban complete` in a terminal is not blocked; the scan reports unchecked results afterward. The gate also does not see commands an executor starts. taskwatch does not isolate what an executor can read or write; path limits require the executor's own permissions or the operating system.

## Standalone job checks

The standalone runner uses the same checklist and review verdict contracts without
opening a Hermes board. The check service runs the approved `check_command` in the
job workspace and records its exit code and complete combined output. It then
verifies required outputs, progress, and checklist.
A model's PASS claim does not replace these checks.

Work and review sessions must settle and report no pending asynchronous work
through public RPC state before the runner advances. Child transcripts and
manifests are not acceptance evidence.

Code results must be committed and clean. Other results use hashes of named
output paths and file contents. Before accepting a review, the runner rechecks
the accepted command log hash, required files, checklist, and every copied input
hash. A review is accepted only for the current result and current intent, spec,
and review profile basis. The review session is separate
from the work session. Re-review includes prior findings, and three automatic
fix rounds are the limit before user judgment. An approved resume into a review
fix reserves the first round of the new cycle before it starts.

The scanner starts ready jobs with free slots, preserves slots between steps,
and holds a target until the old service is confirmed stopped. It records and
recovers dead runs, two-scan stalls, and reboot interruptions. An unreadable
progress or output file parks only its job; unrelated jobs continue scanning.
Missing inputs, corrupt records, unknown service outcomes, exhausted recovery/fix
budgets, approval waits, blocked predecessors, and cancellations prevent automatic completion.
See [standalone configuration](configuration.md#standalone-jobs) for commands and
the timer entry point. Successful checks and review lead to `deliver_pending`;
the scanner then runs the configured delivery command, see
[delivery](configuration.md#delivery). Only a recorded successful delivery makes
the job `done`.
