# Configuration and state

## Approval records

The main agent calls `taskwatch_approve` only after the user approves in the conversation. It writes to the existing `taskwatch/state/approvals.jsonl` under the Hermes root; no separate approval store or configuration key is needed.

- Start: pass `kind: "start"`, `approval_key`, `approved`, and `message_id`. Derive a stable key from the approval message id and pass the same key to `taskwatch_create`. The record contains the approval time, approved scope, key, and message id. Existing start records without a message id remain readable.
- Apply: pass `kind: "apply"`, `card_id`, `approved`, `message_id`, and `fingerprint`. The tool uses the gate's checks against the live result and refuses a mismatched fingerprint or dirty work copy. Recording approval does not apply the result or replace the review and apply gates.

A retry with the same start key returns the original approval. An apply retry with the same message id, card, and fingerprint returns the original approval only if the result still matches. Both avoid appending another line. Lookup and append share the state lock.
