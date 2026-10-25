# Code review

Use this file with the common guidelines when the deliverable is a code change.

## What to review

**K-1.** Review in this order, and finish each stage before the next: the spec and the project rules; correctness and failure modes; consistency of docs, messages, and tests; code quality last.

**K-2.** Read the changed code with its callers and the tests that cover it. A dispatch point outside the diff still has to be read before you treat the producing side as correct.

**K-3.** Keep going after the first blocker. Report every finding that meets the reporting bar.

## Reporting bar

**K-4.** Report an issue only when all of these hold:

- Evidence: a code path, a rule, or a document line. No speculation.
- Actionable: a discrete fix, not a request to consider improving something.
- Unintentional: not a choice the change documents on purpose.
- Introduced here: not a pre-existing problem the change did not touch.
- Proportionate: do not demand a rigor the rest of the codebase does not use.

**K-5.** When nothing meets that bar, the verdict is correct and there are no findings. Do not invent a problem.

## Quality

**K-6.** After the earlier stages, judge quality. A clear violation in the change is a finding. A written project standard wins where it is more specific.

- The edit is no larger than the task needs. Existing files change only where the task needs them to.
- Each new or changed function has one job, and one rule is defined in one place.
- Names say what the thing is.
- A test fails when the behavior it covers breaks.
- A comment explains why, at the density the repository already uses. It does not restate the code.

## Failure modes

**K-7.** Treat a factual claim the change relies on as a claim, not as a fact. If code, tests, or a cited record in the repository do not support it, report the unverified claim when the change depends on it.

**K-8.** For a new type, variant, or value that crosses a function or module boundary, find the consuming dispatch point and confirm an explicit branch or an existing catch-all forwards it. A silent drop, no-op, or discard is a defect.

**K-9.** A fallback that hides the real defect is blocking: a swallowed error, a downgraded diagnostic, a silent default, a forced action that skips a safety check, or a best-effort branch that makes a failure disappear. A narrow fallback is acceptable only when it is scoped to a known external boundary, tested on both paths, and keeps the failure evidence.

**K-10.** Docs, error messages, and tests that still describe the old behavior are findings when the change left them that way.

**K-11.** If the assignment includes test or CI output, a failure caused by the change is a finding. If it does not, say in the verdict that tests were not checked.

## Re-review

**K-12.** On a later round, review the change since the prior reviewed result and the resolution of prior findings. Read surrounding code as needed. Do not re-litigate ground a prior round accepted.

**K-13.** A new blocking finding on previously reviewed ground needs an explicit reason it was not visible before, such as the fix or new evidence. Without that reason, report it as P2 and say so.

**K-14.** Once the prior blockers are resolved, the verdict must not get worse unless a new blocker has that reason. An unresolved prior blocker stays blocking. Do not broaden the scope, add a new requirement, or raise the severity of an item already reported beyond what resolves it.

## Sources

Generalized from a read-only code-review brief. The severity scale and the round rules are in the common guidelines. Provenance is in the [folder README](README.md).
