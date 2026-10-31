# Changelog

All notable changes to hermes-taskwatch are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Code jobs accept an optional `public_text` list of pull request units. The check fails a unit whose commit message has a subject that is not `type(scope): summary` or `type: summary`, a subject or line over 72 characters, no blank line after the subject, or a trailer line, and fails a missing `pr/<id>/title.txt` or `pr/<id>/body.md` in the job folder.
- Code jobs start from a freshly fetched `origin/<default branch>` on a named branch (`<branch_prefix><id8>`, default `taskwatch/`), record `base`, `base_note`, `branch`, `workspace_state` and `branch_state`, and accept an optional `base` field for stacked work. The scan removes a clean worktree at `done` and `cancelled`, keeps a dirty one with the reason, and deletes the branch only once it is merged or squash merged into `origin/<default branch>`.
- Optional `env` object in `jobs-config.json` sets the environment of every process a job starts (omp worker, reviewer, check command, runner git commands); `PATH` entries expand `~`, `$HOME` and `$PATH`, so jobs find tools in `~/.local/bin` without per-job approval.
- A job keeps its checklist and progress files in its job folder instead of the worktree, and the worker prompt gives their absolute paths, so records are never committed into a result and the clean-tree check needs no exception. Records that name `.taskwatch/...` still load.
- `taskwatch watch <job>` blocks until a job reaches `approval_wait`, `user_wait`, `deliver_pending`, `done`, or `cancelled`, prints a short report, and exits (0 attention, 3 time limit, 2 unknown job). It reads `jobs.jsonl` without a lock or write and parses only new lines per poll, so a Hermes background process can wake the room that started the work.
- Optional `review_command` selects a separate executor command for independent job reviews, falling back to `omp_command` when omitted.
- Review-only jobs: submit a `review_target` (files or a commit) to review it without a work stage; the parsed verdict is recorded and delivered.
- `python -m taskwatch.reviewer_launch` builds the omp reviewer command at run time from an omp agent definition and the `docs/review` guidelines, so a review started by taskwatch or by omp alone reads the same guideline text; `review_command` accepts `{guidelines}` and `{review}`.
- Standalone approved job submission, parallel timer scheduling, transient omp RPC runs, interruption recovery, command checks, capped independent review fixes, and configured result delivery from `deliver_pending` to `done` through a literal-argument command with a fixed 60 second timeout. Only confirmed sends finish; every other outcome stops for user judgment without automatic resend. The existing Hermes plugin remains available.
- `taskwatch_approve` records conversational start and apply approvals without hand-written JSON; retries return the existing approval and apply refuses a stale or dirty result.
- `scan` warns through the configured user destination when the board is unreadable and exits 1; config failures exit 1 with the file and cause on stderr for the timer journal.
- Project skeleton: documentation, CI, and issue and pull request templates.
- Review guidelines for code, design, and source-checked documents, with a slot for project rules.
- CI checks that review guidelines keep required sections, local references, and unique rule ids.
- 13 review cases and a scorer that counts missed findings, false positives, severity mismatches, and verdict agreement from a human mapping.
- Hermes plugin entry that pins the tested Hermes version.
- `hermes taskwatch init`, `doctor`, `scan`, and `check`. The timer runs `scan` with the configured profile's `HERMES_HOME`, and `scan` takes the profile from the running Hermes, because Hermes consumes a `--profile` argument itself.
- `scan` sends the user line through Hermes' send path. A line is recorded only after it was delivered, and an unanswered or unsubscribed warning is checked on every scan.
- The checks read a card's exit code, outputs, fingerprint, basis, and reject count from run records and the live result, not from the card body.
- Optional `watch_hook` scan on the Kanban dispatch tick, off unless set.
- The gate blocks `kanban_complete` and `kanban_request_review` when a check fails. It approves only a result whose fingerprint, basis, and clean state match the recorded PASS as they are now, and it freezes exactly the fingerprint it checked.
- `examples/worker/run_executor.py` runs the executor in the foreground, records the run, and reads the review verdict from the review output. All paths in the record are absolute. A card behind an approved gate runs on its own work copy made from the approved snapshot, and is blocked when the gate has no snapshot. A review needs an output written by that run.
- `doctor` fails below the tested Hermes version and warns, without failing, above it.
- `scan`, `check`, the watch hook, the gate, and the card tools use the installed Hermes Kanban board.
- A passing gate approval writes the snapshot path onto the card.
- `taskwatch_create` and `taskwatch_reject` create cards and reject a gate. Create and reject run under the state lock, a failed create archives the children before their parents, and a reject writes its id and the card's last event id before it reopens so a retry finishes a half-done reject. The reject cap counts finished rejects, and a new reject id is refused while another is pending. A review-gate reject is refused, because Hermes has no public way to reopen the fixed worker card together with the review.
- `check --apply` and the apply examples stop when the fingerprint differs. The copy example publishes only a copy it hashed itself.
- Example skill telling a worker to run the executor in the foreground, to hand the verdict line to the review output, and to let the next card run on the approved snapshot.
- README install, update, and removal order, and docs for every config key, every check (what it blocks and when it can be removed), and the seven principles with their sources. Document approval trust boundaries, scan read failures, and known operational limitations.

### Changed

- Code jobs that build a result no longer wait for each other on one target, since each has its own branch and worktree; the slot limit still applies, and non-code and review-only jobs on one target still wait.
- Docs describe the standalone job runner only: README, `docs/checks.md`, `docs/configuration.md`, principles, contributing notes, and the package description no longer mention the removed plugin, cards, or gate, or call the project experimental.
- The plugin no longer changes environment variables, reads Hermes source, or writes the board database directly; profile and paths come from the plugin context and Hermes helpers.
- Generated timer units pin the configured profile with `-p`, and the apply example requires `HERMES_PROFILE`.
- `plugin.yaml` declares `taskwatch_approve`.
- `doctor` no longer checks review-gate reopen support or fails solely because that support is unavailable.
- The job field `kind` is now `review` with values `code`, `design`, and `source` (`document` and `research` merged into `source`), the reviewer launcher flag `--kind` is `--review`, the `review_command` placeholder `{kind}` is `{review}`, and `docs/review/source-checked-document.md` is `docs/review/source.md`; old `jobs.jsonl` records with `kind` still load.

### Removed

- The Hermes Kanban plugin: the plugin entry, cards, completion gate, Hermes tools, the `hermes taskwatch` command, their examples, and the shared-module code only they used. The `taskwatch` job runner is unchanged.

### Fixed

- Verify declared output contents, including Git-ignored files, against PASS before delivery, and send stop notices without holding the global state lock.
- Send one stop notice per wait entry and recheck accepted PASS fingerprints before result delivery.
- Clarify safe plugin updates, default-profile timer pinning, production destinations, review executor responsibility, and user escalation; consolidate operational guidance.
- The enabled dispatch watch hook reports unreadable board evidence through the same user notification path as `scan`, without leaking the read error.
- P0 is only a rule break about runtime behavior or data. Style and wording are P2.
- The guideline check follows angle-bracket and reference-style local links.
- The scorer fails when an answers or mappings directory is missing or has no cases.
