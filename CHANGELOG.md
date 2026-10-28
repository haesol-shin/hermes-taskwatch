# Changelog

All notable changes to hermes-taskwatch are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Standalone approved job submission, parallel timer scheduling, transient omp RPC runs, interruption recovery, command checks, and capped independent review fixes. Successful jobs stop at `deliver_pending`; the existing Hermes plugin remains available.
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

- The plugin no longer changes environment variables, reads Hermes source, or writes the board database directly; profile and paths come from the plugin context and Hermes helpers.
- Generated timer units pin the configured profile with `-p`, and the apply example requires `HERMES_PROFILE`.
- `plugin.yaml` declares `taskwatch_approve`.
- `doctor` no longer checks review-gate reopen support or fails solely because that support is unavailable.

### Fixed

- Revalidate standalone review evidence, reject missing child transcripts and interrupted RPC chunks, preserve collected child transcripts, bound resumed review fixes, and isolate unreadable job progress.
- Clarify safe plugin updates, default-profile timer pinning, production destinations, review executor responsibility, and user escalation; consolidate operational guidance.
- The enabled dispatch watch hook reports unreadable board evidence through the same user notification path as `scan`, without leaking the read error.
- P0 is only a rule break about runtime behavior or data. Style and wording are P2.
- The guideline check follows angle-bracket and reference-style local links.
- The scorer fails when an answers or mappings directory is missing or has no cases.
