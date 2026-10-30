## Launch path: omp alone

This text applies only when a person starts the review directly in omp. It adds to the review guidelines and never changes a judging rule.

**Inputs.** The prompt names what to review: the intent, the spec, and the files or commit under review. Read what the prompt names. If it names no intent or no spec, the review is incomplete.

**Rounds.** The prompt states the round number and, from round 2, the prior reviewed result and the prior findings. Nobody counts rounds for you. If a later round lacks them, say so in the verdict.

**Tools.** You read and run read-only commands. Do not edit the reviewed result. Delegate only to the sub-agents your tools allow, and wait until each one finished.

**Verdict.** Answer in your final message. Its first line is `Verdict: PASS` or `Verdict: FAIL`. `PASS` means no P0 or P1 finding remains; `FAIL` means one remains or the review is incomplete. After that line, give every finding with its location.
