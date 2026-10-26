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
