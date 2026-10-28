# hermes-taskwatch

**Delegate and walk away. Nothing gets dropped, nothing unchecked gets through.**

hermes-taskwatch provides a standalone job runner and a plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) Kanban. The standalone runner schedules approved jobs, checks their outputs, and requests an independent review. The existing plugin watches delegated cards and blocks unchecked results.

> Status: experimental. See [Known limitations](#known-limitations).

## What it is not

- It does not patch or replace Hermes. It registers plugin hooks, commands, and tools.
- The Hermes plugin does not execute work. The standalone runner executes approved jobs through omp's public RPC.
- Every check is plain code and never calls a model. Work, fixes, and independent reviews use omp.

## Standalone jobs

From a checkout of this repository, run `uv sync --locked`, then use `uv run taskwatch`.
The examples below assume `. .venv/bin/activate`; otherwise prefix `taskwatch` with `uv run`.
Hermes is optional. Prepare Linux user systemd, an omp executable for `omp_command`,
and a delivery command before enabling background runs.

```sh
taskwatch --root <root> init
taskwatch --root <root> submit <approved-job.json>
taskwatch --root <root> scan
taskwatch --root <root> show <job-id>
```

Submission returns only after the job and copied inputs are durable. The receipt key
deduplicates retries. `scan` reserves up to the configured number of parallel slots
and starts transient user services. A job keeps its slot across work, check, review,
and fixes. Jobs sharing a target cannot overlap. Approval and user-judgment waits
release their slots after their service has stopped.

Successful jobs stop at `deliver_pending`. The next `scan` writes a delivery message
and runs the configured `deliver_command`, with a fixed 60 second timeout. Only a
recorded success makes the job `done`; it never sends again. A success response means
the command reported success, not that the person read the message. Every other
outcome, including a reported error, stops at `user_wait` with `delivery_unknown`.
It is never resent automatically; the user can resume for one new send or cancel.
Real omp, service recovery, reboot, and real delivery acceptance still need an
authorized installation test. See
[standalone configuration](docs/configuration.md#standalone-jobs) for the submission
format, delivery, installation switch, recovery, and approval commands.

## Install

Supported: Linux with systemd, Python 3.11 or later, no runtime dependencies. macOS and Windows may work through the plugin hook, but they are not tested.

Supported Hermes: 0.21.5, the version the plugin was tested against. `hermes taskwatch doctor` fails on an older Hermes and only warns on a newer one.

Rejecting a review-gate card is not supported, see [Known limitations](#known-limitations).

Do the steps in this order. Stop at the first failure.

1. Use the `default` main profile and install the plugin there and in every worker profile that will run cards. Each profile keeps its own plugin list. First follow the [profile setup](docs/configuration.md#profile-setup) precondition.

   ```sh
   hermes plugins install haesol-shin/hermes-taskwatch --enable
   hermes -p <worker> plugins install haesol-shin/hermes-taskwatch --enable
   ```

   Restart the gateway so it loads the plugin.

2. Write the config draft and the timer units. `--hermes` is the absolute path of the `hermes` executable the timer will run. This writes the units and does not start the timer.

   ```sh
   hermes taskwatch init --hermes "$(command -v hermes)" --board <board>
   ```

   Follow [profile setup](docs/configuration.md#profile-setup) to edit the draft before starting it. The generated service pins the first configured profile. Every key is in [docs/configuration.md](docs/configuration.md).

3. Check the setup.

   ```sh
   hermes taskwatch doctor
   ```

   It checks the config, the board, the destinations, write access to the state directory, the Hermes version and that the plugin is enabled in each listed profile. Every line must be `ok`, except `hermes: warn` on a Hermes newer than 0.21.5, which is informational.

4. Scan a test board once. Point `board` at a board you can throw away and the destinations at a test target first, because a scan writes warning cards and may send a line to `user_notify`.

   ```sh
   hermes taskwatch scan
   ```

5. Switch `board`, `destinations.main_subscription`, and `destinations.user_notify` to their production values. Run `hermes taskwatch doctor` again under the step 3 rule, then start the timer.

   ```sh
   systemctl --user enable --now hermes-taskwatch-scan.timer
   ```

To update: stop the timer and all new Kanban dispatch, then stop the gateway. Wait for existing Kanban workers to finish or explicitly terminate them; stopping the gateway does not guarantee their exit. Only after no workers remain, replace the plugin in each profile with `hermes plugins update hermes-taskwatch`, run `hermes taskwatch doctor`, restart the gateway and dispatch, then start the timer. Do not disable the plugin during an update, because a disabled plugin is not loaded and `doctor` does not exist.

To remove: stop and disable the timer, disable the plugin in each profile, then remove it with `hermes plugins remove hermes-taskwatch`. Delete `hermes-taskwatch-scan.service` and `hermes-taskwatch-scan.timer` from `~/.config/systemd/user/` and reload systemd. The state directory is kept, see [docs/configuration.md](docs/configuration.md#state-directory).

## The problem

You give an agent a long job and leave the chat. Hours later you find out that a step stalled, the next step never started, or a result was marked done without the review you asked for. Nobody told you, because nothing was watching the work between your messages.

## How it works

- **Intent and spec.** Each card points to an `intent.md` (why, in your words, and when it is finished) and a `spec.md` (deliverables, checklist, out of scope). The main agent creates cards with `taskwatch_create`, which refuses a card when a section is missing: `Source`, `Purpose`, `Done`, `Approval` in the intent, and `Outputs`, `Checklist`, `Out of scope`, `Live`, `Guidance`, `Model` in the spec.
- **Gate before completion and review requests.** When an agent in a listed profile calls `kanban_complete` or `kanban_request_review`, the plugin checks the exit status, required outputs, and checklist. Approving a gate card also needs a passing review for the exact current result. If a check fails, the tool call is blocked with the reason.
- **Scope.** See [Not covered](docs/checks.md#not-covered) for terminal bypasses and executor permissions.
- **Review invocation.** A review makes a separate call to the configured executor. For independent model context, configure that executor to create a fresh session. The record keeps the fingerprint of the result and the basis it was reviewed against, so an old pass does not count for a changed result.
- **Approval bound to a result.** An apply approval is valid only for the fingerprint you approved. `examples/apply/merge_pr.sh` merges with `gh pr merge --match-head-commit <approved SHA>`, so GitHub rejects a head that changed after approval, and `examples/apply/copy_snapshot.py` copies only a snapshot whose hash is the approved one.
- **Watch in the background.** A systemd timer scans the board and warns the main agent about stalled cards, missing next steps, and results that skipped a check. See [warning delivery](docs/checks.md#how-a-warning-is-delivered) for user escalation.
- **Rejects have an end.** `taskwatch_reject` reopens a gate for another round, once per reject id, up to a cap. At the cap the card stays in review for you to decide.

## Commands

```sh
hermes taskwatch init --hermes <path> [--board <slug>] [--root <dir>] [--unit-dir <dir>]
hermes taskwatch doctor                  # check config, board, destinations, and Hermes
hermes taskwatch scan                    # scan the board once
hermes taskwatch check <card>            # show whether a card can move on, and why
hermes taskwatch check <card> --apply    # print the approved fingerprint, or fail if it differs
```

The main agent uses `taskwatch_approve` for [approval records](docs/configuration.md#approval-records), `taskwatch_create` to create cards, and `taskwatch_reject` to reject a gate. See [scan read failures](docs/configuration.md#scan-read-failures) for notification and exit behavior.

## Examples

- [examples/worker/run_executor.py](examples/worker/run_executor.py) runs the executor in the foreground and records the run that the gate reads.
- [examples/skills/taskwatch-worker/SKILL.md](examples/skills/taskwatch-worker/SKILL.md) tells a Kanban worker how to use it.
- [examples/apply](examples/apply) holds the two apply scripts.

## Known limitations

- A retried delivery of the same wake makes the main agent write one judgment comment per delivery. There is no comment deduplication layer. The reject itself stays idempotent when the same stable `reject_id` is reused.
- Rejecting a review-kind gate card is not supported, because Hermes has no public atomic reopen. `taskwatch_reject` refuses with `core update needed` and changes nothing. Work-gate rejection is supported.
- Hermes consumes taskwatch's `--profile` option as its global profile selector. See [profile setup](docs/configuration.md#profile-setup) for the workaround and timer pinning.
- After a plugin hook timeout, Hermes temporarily suppresses the same callback. The `pre_tool_call` guard fails closed during suppression, so even `kanban_block` is refused. A card can stay running until the worker exits and the dispatcher reclaims its dead claim.
- Only a scripted model was used to exercise agent behavior. Behavior with a real model has not been tested.

## More

- [docs/configuration.md](docs/configuration.md): every config key and the state directory.
- [docs/checks.md](docs/checks.md): what each check blocks and when it can be removed.
- [docs/principles.md](docs/principles.md): the rules behind the design and their sources.
- [docs/review](docs/review/README.md): review guidelines.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
