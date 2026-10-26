# hermes-taskwatch

**Delegate and walk away. Nothing gets dropped, nothing unchecked gets through.**

hermes-taskwatch is a plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) Kanban. It is meant to watch delegated work, block unchecked results from moving on, and wake the main agent or you only when a decision is needed.

> Status: experimental. The plugin entry and `hermes taskwatch` commands are in the tree. A real install is not the acceptance test.

## What it is not

- It does not modify or replace Hermes. It uses the public plugin API only.
- It does not do the work. It never reruns, merges, or edits task cards for you.
- It does not call a model. Every check is plain code.

## The problem

You give an agent a long job and leave the chat. Hours later you find out that a step stalled, the next step never started, or a result was marked done without the review you asked for. Nobody told you, because nothing was watching the work between your messages.

## How it will work (planned)

- **Intent and spec.** Each card points to an `intent.md` (why, in your words, and when it is finished) and a `spec.md` (deliverables, checklist, out of scope).
- **Gate before completion and review requests.** When an agent in a configured profile calls `kanban_complete` or `kanban_request_review`, the plugin checks the exit status, required outputs, and checklist. Approving a gate card also needs a passing review for the exact current result. If a check fails, the tool call is blocked with the reason.
- **Limit.** The gate covers agent tool calls only. A person running `hermes kanban complete` in a terminal is not blocked; the background scan reports it afterward.
- **Independent review.** Reviews run in a separate read-only session of your executor. Review records are kept with the result fingerprint, so an old pass does not count for a changed result.
- **Approval bound to a result.** An apply approval is valid only for the commit or files you approved. Merges use `gh pr merge --match-head-commit <approved SHA>`, so GitHub rejects a head that changed after approval.
- **Watch in the background.** A systemd timer scans the board and warns about stalled cards, missing next steps, and results that skipped a check.

## Commands (planned)

```sh
hermes taskwatch init          # write a config draft and install the timer
hermes taskwatch doctor        # check config, board, destinations, and Hermes version
hermes taskwatch scan          # scan the board once
hermes taskwatch check <card>  # show whether a card can move on, and why
```

The main agent uses `taskwatch_approve` to record approval only after you approve in the conversation, `taskwatch_create` to create cards, and `taskwatch_reject` to reject a gate. For a start approval, pass `kind: "start"`, a stable `approval_key` derived from the approval message id, `approved` (what you approved), and `message_id`; then create the cards with that key. For an apply approval, pass `kind: "apply"`, `card_id`, `approved`, `message_id`, and the current result `fingerprint`. A changed or dirty result is refused. Repeating a start key or an apply approval for the same message, card, and fingerprint returns the existing record without another line.

## Requirements

- Linux with systemd is the supported target. macOS and Windows may work through the plugin hook, but they will not be tested.
- Python 3.11 or later. No runtime dependencies.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
