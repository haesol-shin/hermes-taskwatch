# Changelog

All notable changes to hermes-taskwatch are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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

### Fixed

- P0 is only a rule break about runtime behavior or data. Style and wording are P2.
- The guideline check follows angle-bracket and reference-style local links.
- The scorer fails when an answers or mappings directory is missing or has no cases.
