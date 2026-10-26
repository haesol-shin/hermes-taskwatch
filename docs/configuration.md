# Configuration and state

## Approval records

The main agent calls `taskwatch_approve` only after the user approves in the conversation. It writes to the existing `taskwatch/state/approvals.jsonl` under the Hermes root; no separate approval store or configuration key is needed.

- Start: pass `kind: "start"`, `approval_key`, `approved`, and `message_id`. Derive a stable key from the approval message id and pass the same key to `taskwatch_create`. The record contains the approval time, approved scope, key, and message id. Existing start records without a message id remain readable.
- Apply: pass `kind: "apply"`, `card_id`, `approved`, `message_id`, and `fingerprint`. The tool uses the gate's checks against the live result and refuses a mismatched fingerprint or dirty work copy. Recording approval does not apply the result or replace the review and apply gates.

A retry with the same start key returns the original approval. An apply retry with the same message id, card, and fingerprint returns the original approval only if the result still matches. Both avoid appending another line. Lookup and append share the state lock.

## Scan read failures

An unreadable board prevents a clean scan: the command exits 1 and uses `destinations.user_notify` through the normal Hermes send path. No warning card is written to the broken board. The existing user rewarn rule applies: the same cause and summary are suppressed after successful delivery, including across restarts; a changed summary is sent again. Failed delivery is not recorded and is retried on the next scan.

With `watch_hook: true`, dispatch-tick scans use the same read-failure notification path. Unreadable board evidence is reported to `destinations.user_notify` and returned as a diagnostic line instead of escaping the hook as an exception.

If `taskwatch/config.json` is missing, unreadable, or invalid, `scan` cannot determine a destination. It exits 1 and writes one stderr line containing the file path and failure cause. The systemd timer's service journal retains stderr; inspect it with `journalctl --user -u hermes-taskwatch-scan.service`. No fallback destination is used.
