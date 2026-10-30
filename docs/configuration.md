# Configuration and state

taskwatch reads one file, `<Hermes root>/taskwatch/config.json`. `hermes taskwatch init` writes a draft of it. A missing key, an unknown key, or an unknown `version` is refused before any check runs, because a silent default could point the watch at the wrong target.

The Hermes root is the directory that holds the profiles. When `HERMES_HOME` is `<root>/profiles/<name>`, the root is `<root>`, so the main profile and every worker profile read the same file. `--root` on a command overrides it.

Every key below is required. There are no defaults in the reader; table values are illustrative synthetic examples, not literal `init` output.

## Top level

| Key | Type | Meaning | Example |
| --- | --- | --- | --- |
| `version` | integer | Format version. Only `1` is accepted. | `1` |
| `hermes_root` | string, absolute path | The Hermes root this config is for. A watch started from another root only warns that the target does not match. Write the resolved path, because the comparison is exact. | `"/srv/hermes"` |
| `board` | string | Slug of the one Kanban board to watch. | `"default"` |
| `profiles` | list of strings | Hermes profiles that use the board: the main profile and each worker profile. Not empty, no duplicates. `default` is the root itself, any other name is `<root>/profiles/<name>`. A profile outside this list is never gated. A profile inside it that cannot be resolved is blocked. The first entry selects the `HERMES_HOME` written by `init` and the profile used by `check`; editing the list does not rewrite the unit. | `["default", "worker"]` |
| `watch_hook` | boolean | Also scan on the Kanban dispatch tick. Off unless `true`. It shares the lock and the warning history with the timer. | `false` |

### Profile setup

The README installation assumes the main profile is `default`, `<Hermes root>/active_profile` selects `default`, and the plugin is installed there. Select it with `hermes profile use default` before following the unqualified commands.

Initialize without taskwatch's `--profile` option: Hermes consumes both `--profile <name>` and `--profile=<name>` globally before the plugin sees them. Edit `profiles` in the generated config to include the main and worker profiles, keeping `default` first for this setup. This list controls taskwatch checks, not Hermes' global profile selection.

The draft has `profiles: ["default"]`. Both destination strings initially contain the same placeholder spec (`platform: "placeholder"`, `chat_id: "chat.invalid"`, `thread_id: "0"`, `chat_type: "dm"`, `profile: "default"`, `delivery_mode: "wake"`). Replace the destinations and executor commands before use.

The generated service sets `HERMES_HOME` and pins `profiles[0]` with `-p`, so the timer does not follow `active_profile`. With the draft configuration, its command uses the absolute executable path supplied to `init`:

```ini
ExecStart=<hermes> -p default taskwatch scan
```

`init` reloads the user units. After changing `profiles[0]`, edit the existing service's `HERMES_HOME` and `ExecStart -p` together to match the new profile, then run `systemctl --user daemon-reload`.

## `destinations`

A destination is a JSON subscription spec stored as a string. `platform` and `chat_id` are required inside it. `thread_id`, `chat_type`, `profile`, and `delivery_mode` are optional strings. Any other key is refused by `doctor` and by the subscribe step.

| Key | Type | Meaning | Example |
| --- | --- | --- | --- |
| `destinations.main_subscription` | string (JSON spec) | Where the main agent is woken. It is subscribed on every gate card and on every warning card. The gate-subscription check compares this exact string with what the card stores. | `"{\"platform\":\"chat\",\"chat_id\":\"room.invalid\",\"delivery_mode\":\"wake\"}"` |
| `destinations.user_notify` | string (JSON spec) | Where the person is told. Delivered through the Hermes send path, as `hermes send` does. | `"{\"platform\":\"chat\",\"chat_id\":\"person.invalid\"}"` |

## `executors`

Each template is a non-empty argument list. It is run without a shell. A placeholder is replaced only when it is a whole argument, and a placeholder inside a longer argument is refused. Every other argument is passed as written.

| Key | Type | Meaning | Example |
| --- | --- | --- | --- |
| `executors.work` | list of strings | Command for a work run. Placeholders: `{brief}` (the delegation file), `{copy}` (the work copy that will hold the result), `{output}` (where the executor writes its own report). | `["executor", "--brief", "{brief}", "--copy", "{copy}", "--output", "{output}"]` |
| `executors.review` | list of strings | Command for a review run. Same placeholders. The reviewer's output must hold one verdict line, see [Review records](checks.md#review-records). | `["executor", "--review", "--brief", "{brief}", "--output", "{output}"]` |

## `times`

Every value is a whole number of seconds, at least 1, except `rewarn_backoff`.

| Key | Type | Meaning | Example |
| --- | --- | --- | --- |
| `times.gate_action_seconds` | integer | How long a gate card may sit after its latest review request with no approve, reject, or valid wait before it is reported. | `1800` |
| `times.reject_followup_extra_seconds` | integer | Time added to the card's own time limit before a started reject that has no new review request is reported. | `3600` |
| `times.next_start_seconds` | integer | How long after a handled `next` comment the named card has to start. | `1800` |
| `times.unstarted_after_parents_seconds` | integer | How long a card with no run may stay unstarted after all its parents are done. | `3600` |
| `times.start_approval_without_card_seconds` | integer | How long a start approval may exist with no linked card. | `1800` |
| `times.rewarn_initial_seconds` | integer | Wait before the second warning of one cause. Each later wait is the previous one multiplied by `rewarn_backoff`. | `1800` |
| `times.rewarn_backoff` | integer, at least 2 | Multiplier for the wait between warnings of one cause. Not a number of seconds. | `2` |
| `times.user_notify_unreceived_seconds` | integer | How long after the latest warning, with no received reply on it, before the person is told. | `600` |
| `times.stall_unchanged_seconds` | integer | How long a running card's progress file and output files may stay unchanged. | `1800` |

## `limits`

| Key | Type | Meaning | Example |
| --- | --- | --- | --- |
| `limits.reject_cap` | integer | Finished rejects one gate card may have. The next reject is refused and the card stays in review for a person to decide. | `3` |
| `limits.rewarn_cap` | integer | Warnings of one cause that go to the main agent. At the cap the person is told and no more warnings are written for that cause. | `3` |

## `comments`

The main agent leaves short comments on a card to say what it did. The checks read these exact words, so the wording is yours to choose and the main agent's instructions must use the same wording. A handled comment reads `<handled_prefix><event id> action=<action>`.

| Key | Type | Meaning | Example |
| --- | --- | --- | --- |
| `comments.handled_prefix` | string | Start of a comment that says an event was handled. | `"handled event="` |
| `comments.received_prefix` | string | A comment on a warning card that is exactly this prefix followed by the cause id. It says the main agent got the warning. | `"received alert="` |
| `comments.approve` | string | Action word for an approval. | `"approve"` |
| `comments.reject` | string | Action word for a reject. | `"reject"` |
| `comments.wait_prefix` | string | Action prefix for a wait. The rest is the id of the card being waited on. The wait counts while that card is open, or while the gate card itself is blocked. | `"wait:"` |
| `comments.next_prefix` | string | Action prefix that names the next card to start. The rest is that card's id. | `"next:"` |

## State directory

State lives in `<Hermes root>/taskwatch/state/`. The timer, the dispatch hook, and the gate in every profile use the same directory, one lock, and one warning history. Records are one JSON object per line, each with a format version. A line is only appended, never rewritten.

An unreadable committed line, or a record version the code does not know, makes the file unreadable. A scan then reports that the board cannot be checked, and the gate refuses. A last line with no newline is kept beside the file as `<name>.torn-<time>` and the file is cut back before the next write.

| Path | Holds | Written by |
| --- | --- | --- |
| `lock` | The one file lock. | every writer |
| `approvals.jsonl` | `start` lines (approval key, time, what was approved, message id), `link` lines (approval key, card id), and `apply` lines (what was approved, message id, card id, result fingerprint). | `taskwatch_approve` (`start` and `apply` lines), `taskwatch_create` (`link` lines) |
| `reviews.jsonl`, `reviews/bodies/` | One line per review round (card, fingerprint, basis, reviewer, verdict, blocking) and the review text it was read from. | the example worker script |
| `runs.jsonl`, `runs/<run id>.log` | One line per executor run (exit code, work copy, result files, checklist, progress file) and the executor's own output. | the example worker script |
| `rejects.jsonl` | A `started` and a `done` line per reject id. | `taskwatch_reject` |
| `warnings.jsonl` | One line per warning sent, per cause and channel. | `scan` |
| `snapshots/<fingerprint>/` | A frozen copy of a result that passed a gate, named by its fingerprint. | the gate, when it approves |

Removing the plugin does not remove this directory.

### Approval lines

Each line is a JSON object with `version` set to `1`. Writers take the lock, repair a torn tail, and sync the file. Tool usage, trust boundaries, and retry behavior are in [Approval records](#approval-records).

| `kind` | Keys | Rules |
| --- | --- | --- |
| `start` | `approval_key`, `approved_at`, `approved`, optional `message_id` | `approval_key` and `message_id` are ids of letters, digits, `.`, `_`, `:`, `-`, starting with a letter or digit. `approved_at` is UTC and ends in `Z`. `approved` is one line of text. Older lines without `message_id` stay readable. |
| `link` | `approval_key`, `card_id` | Needs an existing start and cannot name a second card. |
| `apply` | `approved`, `message_id`, `card_id`, `fingerprint` | Stores the card and result identity. |

## Approval records

The `taskwatch_approve` tool records an approval in `taskwatch/state/approvals.jsonl` under the Hermes root. No separate approval store and no configuration key is involved. The main agent MUST call it only after the user approves in the conversation. The tool cannot verify that the user approved, so the line records what the caller says. `message_id` is the id of the user's approving message and is stored in the record.

- Start: pass `kind: "start"`, `approval_key`, `approved`, and `message_id`. Derive a stable key from the approval message id and pass the same key to `taskwatch_create`, which refuses a key with no start line and links the key to the card it makes. The record holds the approval time, the approved text, the key, and the message id.
- Apply: pass `kind: "apply"`, `card_id`, `approved`, `message_id`, and `fingerprint`. Before writing, the tool reads the board and runs the same `clean_result` and `apply_match` checks as the gate against the live card. It refuses when the board cannot be read, when the work copy has uncommitted changes, or when `fingerprint` is not the card's current fingerprint. For a Git result the fingerprint is the commit, so the approval is bound to the card, the fingerprint, and that commit. Recording an approval does not apply anything and does not replace the review and apply gates.

A retry with a start key that already has a start line returns the original record and appends nothing, even if the other arguments differ. An apply retry with the same message id, card, and fingerprint returns the original record and appends nothing, but only after the live checks above pass again. Lookup and append share the state lock. On failure the tool returns `{"ok": false, "error": ...}` and writes nothing.

## Scan read failures

An unreadable board prevents a clean scan: the command exits 1 and uses `destinations.user_notify` through the normal Hermes send path. No warning card is written to the broken board. The existing user rewarn rule applies: the same cause and summary are suppressed after successful delivery, including across restarts; a changed summary is sent again. Failed delivery is not recorded and is retried on the next scan.

With `watch_hook: true`, dispatch-tick scans use the same read-failure notification path. Unreadable board evidence is reported to `destinations.user_notify` and returned as a diagnostic line instead of escaping the hook as an exception.

If `taskwatch/config.json` is missing, unreadable, or invalid, `scan` cannot determine a destination. It exits 1 and writes one stderr line containing the file path and failure cause. The systemd timer's service journal retains stderr; inspect it with `journalctl --user -u hermes-taskwatch-scan.service`. No fallback destination is used.

## Standalone jobs

The standalone `taskwatch` command never imports Hermes. `--root <root>` selects
the root explicitly; otherwise `HERMES_HOME`, then `~/.hermes`, is used. Select
the shared root, not a worker profile root. State uses the same
`<root>/taskwatch/state/lock` as the plugin.
Use persistent, nonsynchronized storage for operational state. Temporary
directories are suitable only for synthetic tests.

`taskwatch --root <root> init` writes `taskwatch/jobs-config.json` if absent:

```json
{
  "version": 1,
  "slots": 2,
  "stall_seconds": 1800,
  "omp_command": ["omp"],
  "rpc_timeout": 3600,
  "deliver_command": ["false", "{message}"]
}
```

All keys shown above are required. The three numeric settings are positive integer counts
or seconds. `omp_command` is an argument list prefix. Checks use the submission's
`check_command`, not this model command. `deliver_command` is described in
[Delivery](#delivery). The draft value
`["false", "{message}"]` is intentionally disabled: it can never send, so edit it before use.

Optionally add `"review_command": ["executor", "--role", "reviewer"]` to run
independent reviews with a different command prefix. Like `omp_command`, it must
be a nonempty list of nonempty strings. When omitted, reviews use `omp_command`;
work and fix always use `omp_command`. Both commands receive `--mode rpc --no-ui`.
The `init` draft omits this optional key.

In `review_command` (and only there), an argument that is exactly `{guidelines}` becomes the
job's own copy of the review guidelines, and exactly `{review}` becomes the job's `review`
value. That lets the review command be the [reviewer launcher](review-launch.md), which
reads the agent definition and those guidelines when the review starts:

```json
"review_command": ["python3", "-m", "taskwatch.reviewer_launch", "--agent", "/srv/example/reviewer.md",
                   "--guidelines", "{guidelines}", "--review", "{review}"]
```

### Submit an approved job

Pass a JSON file to `taskwatch --root <root> submit <file>`. Example:

```json
{
  "receipt_key": "approval-message-1:job-1",
  "approval_source": "approval-message-1",
  "approval_scope": "Create local deliverables only",
  "prompt": "Write the approved deliverable and record execution evidence.",
  "review": "source",
  "target": "/srv/example-target",
  "intent": "/srv/example-inputs/intent.md",
  "spec": "/srv/example-inputs/spec.md",
  "inputs": {
    "work": ["/srv/example-inputs/context.md"],
    "review": ["/srv/example-inputs/review-context.md"]
  },
  "outputs": ["result.md"],
  "check_command": ["sh", "check-result.sh"],
  "checklist": "checklist.json",
  "progress": "progress.md",
  "predecessors": [],
  "expected_wait_seconds": 0,
  "approval_required": false
}
```

`target` must exist. `review` picks the review guidelines the reviewer reads: `code`,
`design`, or `source` (a document that cites sources, such as research notes, summaries,
or comparisons). Only `code` also runs the work in a Git worktree, because a code review
reads the Git diff.
Outputs, checklist, and progress paths are relative to the job workspace and
cannot traverse outside it. `check_command` runs as argv without an implicit
shell; name a shell explicitly only when the approved check needs one.
The work must create that check script if it is not already in its workspace.
The checklist follows the existing nonempty `items` list of boolean `passes`.

`inputs`, `predecessors`, `expected_wait_seconds`, and `approval_required` are
optional. Every other example field is required. Predecessors must already
exist. Derive the receipt key from the approval message and its job number.
A repeated key returns the original job even if other arguments differ.
The command records the caller's approval assertion; it cannot authenticate
the conversation.

`approval_required` defaults to `false`. If `true`, the first scan moves the job
to `approval_wait` before its first work run. `approve` clears that flag and
allows work to start. A later `wait` records the current step for approval.
`approval_scope` is a recorded boundary, not operating-system isolation.
The runner's shell tools have the user's permissions; instructions and
explicit approval records cannot enforce those permissions.

The check command must exit `0`. Required outputs and a nonempty passing
checklist must also exist after the command finishes. Minimal checklist:

```json
{"items": [{"passes": true}]}
```

No other checklist item fields are required. A `false` item, missing file,
invalid JSON, or empty list blocks progress. The independent reviewer returns
exactly one verdict JSON line followed by findings, for example:

```text
{"verdict": "PASS", "blocking": false, "reviewer": "independent-review"}
No blocking findings.
```

A FAIL, malformed verdict, missing input, or blocking finding prevents
`deliver_pending`. See [review records](checks.md#review-records) for the shared contract.

Submission copies and hashes intent, spec, and role-specific inputs. The
original prompt is kept intact. `jobs.jsonl` stores append-only, checksummed
snapshots. The existing torn-tail repair preserves a partial last line before
the next append. Corrupt committed records block scheduling rather than
letting a job start or pass. Inspect the scan service journal for diagnostics.

Each job has `state/jobs/<id>/`, a separate workspace, copied inputs, a
`WORKFLOW.md`, and execution evidence. Code jobs use a detached git worktree
at the target's HEAD. Results remain there for inspection; this command
does not merge or apply them.

#### Review-only jobs

Replace `outputs`, `check_command`, `checklist`, and `progress` with one `review_target` to
review something that already exists. There is no work stage and no check; the job starts at
the review step, in the target directory itself.

```json
{
  "receipt_key": "approval-message-1:review-1",
  "approval_source": "approval-message-1",
  "approval_scope": "Review only; change nothing",
  "prompt": "Review this change against the intent and the spec.",
  "review": "code",
  "target": "/srv/example-repo",
  "intent": "/srv/example-inputs/intent.md",
  "spec": "/srv/example-inputs/spec.md",
  "review_target": {"commit": "HEAD"}
}
```

`review_target` is exactly one of `{"files": ["relative/path"]}` (files under `target`) or
`{"commit": "<revision>"}` (resolved to a full SHA when the job is submitted). `inputs`,
`predecessors`, `expected_wait_seconds`, and `approval_required` work as for other jobs.

The verdict is the result. A parsed `PASS` or `FAIL` is recorded and delivered; a `FAIL` does
not start a fix, because there is nothing to fix. An answer with no acceptable verdict line, or a
target that changed during the review, costs a round and reviews again. The cap is the one other jobs have: the first review plus three
retries, four reviews in all; after that the job stops for user judgment (`review_exhausted`). The review record and
job state are in `show <id>`.

### Timer entry point

The plugin commands and its current units continue to work unchanged.
Do not register a job in both the Hermes board and the standalone job board.
Stop new legacy dispatch and let existing cards finish before replacing the
timer entry point. Preserve the five-minute timer schedule.

`taskwatch --root <root> init --unit-dir <empty-unit-staging-directory>` writes
`hermes-taskwatch-scan.service` and `.timer` without installing, reloading, or
enabling them. Existing files are refused. The service calls the standalone
`scan` with the current interpreter and explicit root. Inspect the staged files
and replace the existing service only as part of an approved installation.
Run `systemctl --user daemon-reload` after installing it. Confirm the timer is
enabled and active, and confirm linger with `loginctl show-user <user> -p Linger`
if jobs must survive logout. Run these installation steps only on the intended
host with approval.

`scan` is a short scheduler, not a resident dispatcher. It reserves the job
attempt durably before invoking `systemd-run --user`. The deterministic
`taskwatch-<id>-a<attempt>.service` name allows a later scan to find a start
whose acknowledgement was lost. `taskwatch run <id> --attempt <n>` is an
internal step command; do not run it alongside the service.

### Installation switch

Do these steps in order on the intended host, with approval. Stop at the first failure.

1. Stop new legacy dispatch and let existing cards finish. Do not register a job in both the Hermes board and the standalone job board. If `<root>/taskwatch/jobs-config.json` already exists, add `deliver_command` to it before running `init` (see [Delivery](#delivery)); `init` validates existing config without changing it. Fresh installations can keep the init-then-edit order below.
2. Stage the units: `taskwatch --root <root> init --unit-dir <empty-unit-staging-directory>`. This also writes `jobs-config.json` if absent. Nothing is installed or enabled.
3. Edit `<root>/taskwatch/jobs-config.json`, including `deliver_command` (see [Delivery](#delivery)).
4. Inspect the staged `hermes-taskwatch-scan.service` and `.timer`, install them into `~/.config/systemd/user/` (replacing the legacy units only as part of this approval), then run `systemctl --user daemon-reload`.
5. Enable the timer with `systemctl --user enable --now hermes-taskwatch-scan.timer`. Confirm it is enabled and active, and confirm linger with `loginctl show-user <user> -p Linger` if jobs must survive logout.
6. Submit jobs with `taskwatch --root <root> submit <approved-job.json>`.

### Delivery

The `init` draft sets `deliver_command` to `["false", "{message}"]`, which
intentionally cannot send. An attempt with this draft stops for user judgment,
so replace it before enabling the timer.

`jobs-config.json` requires `deliver_command`: a list of nonempty strings run as
argv without a shell. Exactly one argument must be the whole string `{message}`;
it is replaced with the absolute path of the generated message file. The
destination is a literal argument written in this command, not the job's
`target` (a workspace path). Example, with a synthetic destination:

```json
"deliver_command": ["hermes", "send", "--to", "discord:#synthetic-channel", "--file", "{message}", "--json"]
```

Choose a configured Hermes target with `hermes send --list`.

Hermes is optional. Without it, provide a sender executable for your channel and
configure its argv instead, for example
`["notify-command", "--to", "person.invalid", "--file", "{message}"]`.
The sender must wait for the channel server's response and implement this contract:
stdout must contain exactly one JSON object, with no diagnostic text around it.
Write diagnostics to stderr. Both streams and the exit code are recorded.
For example, exit 0 with `{"success": true, "message_id": "synthetic-message"}`
confirms sending. Every other outcome is unknown, including a nonzero exit with
a readable JSON `error`: a timeout or partial send may already have delivered content.

For a job at `deliver_pending`, `scan` runs this step under the state lock after
the old unit has been released. It writes `delivery.txt` (title from the first
prompt line, job id, latest review verdict, absolute output paths, result
workspace, review body file, and check log path) and `delivery-review.txt` in the
job folder, records the attempt as `started` with the message file and the
SHA-256 of `delivery.txt`, then runs the command with a fixed 60 second timeout.

Before sending, the result fingerprint, declared output content digest, and review
basis must match the accepted PASS. The output digest also covers Git-ignored files.
Changed, missing, or unreadable evidence returns to `check`, spending the existing
recovery budget; exhaustion stops at `user_wait/result_unstable`.

After scheduling work and result deliveries, each scan sends one short stop
notice for every new `user_wait` or `approval_wait` entry through the same
`deliver_command`, timeout, and success contract. `notice-<index>.txt` names the
job, first prompt line, stage, wait reason, error, and resume/approve/cancel
commands. Attempts are reserved in `notices` under the state lock before sending.
The sender runs without that lock; only its notice result is merged into the latest
snapshot, so unrelated jobs can finish while it waits. No outcome causes an automatic
retry or changes the stage. Interrupted attempts become unknown.
`delivery_unknown` receives a separate notice, never a result resend.

For result delivery, success needs exit code 0, a JSON object with a truthy
`success`, no `error`, and a present `message_id`. Then the job becomes `done` and is never sent again.
Success means the command reported acceptance; it is not a read receipt. A
response with only `skipped` is unknown, not done.

- Every non-confirmed outcome (including a readable `error`, timeout, unreadable
  output, signal, ambiguous response, or an earlier attempt still `started`)
  moves the job to `user_wait` with reason `delivery_unknown`. It is never resent
  automatically. Hermes provides neither lookup nor an idempotency key.
- `resume <id>` restores `deliver_pending` for one new send and keeps the attempt
  history; an interrupted `started` attempt is recorded as unknown. Resume only
  when a duplicate message is explicitly accepted, because the earlier message
  may have been delivered. `cancel` prevents further sends.

### Watching a job

`taskwatch --root <root> watch <job-id> [--timeout <seconds>] [--interval <seconds>]` blocks
until the job stage is `approval_wait`, `user_wait`, `deliver_pending`, `done`, or
`cancelled`, prints a report of at most 20 lines (id, stage, `wait_reason`, whether an `error` is recorded (its text is not printed), last
review verdict, output and detail paths, next commands), and exits. It exits at once when the
job already stopped. Exit codes: `0` needs attention, `3` the `--timeout` (default 82800)
passed with nothing to report, `2` usage error or unknown job, `1` other error. `--interval`
defaults to 30 and is at least 5. The command is read only: it takes no lock and writes
nothing. It reads `jobs.jsonl` once, then only the lines appended since the last poll, because
a full load of a large ledger takes about a second. Hermes starts it from the room that
submitted the job:

```python
terminal(background=True, notify_on_complete=True, command="taskwatch --root <root> watch <id>")
```

The report is a hint; re-read the state with `show` before acting. `approve` and `resume`
move the job to `queued`, so a watcher started after them waits for the next stop.

### Recovery and decisions

The job retains its parallel slot through work, checks, review, and fixes.
The target is locked across these stages. A blocked predecessor leaves
the successor queued. An unrelated target can still start.
Dead or reboot-interrupted runs resume only after the old service is known
to have stopped. An unknown service outcome requires user judgment.
Unchanged progress and output content first produces a stall suspicion;
another unchanged scan stops the service and recovers. Log growth alone is
not progress. Expected waits delay stall recovery.

Three consecutive failed recoveries stop for user judgment. Passing checks
reset that recovery budget. The independent review has a separate per-cycle
cap of three automatic fix rounds. A fresh review uses `docs/review/` profiles
and the existing verdict JSON contract. Fingerprints and review basis are
computed by code; changing either invalidates the earlier pass.

```sh
taskwatch --root <root> wait <id> --reason "Unapproved external write"
taskwatch --root <root> approve <id> --reason "approval-message-2" --scope "Exact approved scope"
taskwatch --root <root> resume <id> --reason "Approved a new fix cycle"
taskwatch --root <root> cancel <id> --reason "User cancelled"
```

Wait and cancel fence late output immediately. The scanner confirms service
termination before reusing the slot or target. Approval resumes the saved step;
an explicit user resume opens a new recovery/fix cycle. Resuming a review fix
reserves its first round before launch. Check repairs keep the separate recovery
budget. Commands do not send messages themselves; scans send results and stop
notices. External outcome reconciliation is outside this version.

`approve --scope` records a replacement approved scope together with the approval
source. Omit it to preserve the original scope, such as an initial approval wait.
Do not use a broader scope than the user actually approved.

After checks and review pass, the stage is `deliver_pending`, not `done`; see
[Delivery](#delivery). Real-install acceptance is outside this version.
