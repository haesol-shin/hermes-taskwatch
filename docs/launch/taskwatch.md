## Launch path: taskwatch job

This text applies only when a taskwatch job started the review. It adds to the review guidelines and never changes a judging rule.

**Inputs.** Everything the assignment gives you comes from the message above: the intent file, the spec file, the reviewed result or target, the original request, and any review inputs. Read those paths. The check output, when named, is the result of the deterministic check; it is not a verdict. Do not look for other assignment inputs; the repository rules the guidelines name are still read.

**Rounds.** Taskwatch counts the rounds and states the round number in the message. It enforces the round cap itself and does not tell you the cap, so the round-cap rule of the guidelines has nothing to act on in your review: judge the round in front of you. A round whose answer cannot be accepted still counts as a round.

**Tools.** You read and run read-only commands. Do not edit the reviewed result, and write review evidence only under the folder the message names. Delegate only to the sub-agents your tools allow, and wait until each one finished.

**Verdict.** Answer in your final message. It must contain exactly one line that is a JSON object:

{"verdict": "PASS" or "FAIL", "blocking": true or false, "reviewer": "<name>"}

- `PASS` means no P0 or P1 finding remains. A `PASS` cannot be blocking.
- `FAIL` means a P0 or P1 finding remains, or the review is incomplete.
- Write no other line that starts with `{` and contains `"verdict"`.

After that line, give every finding with its location. Taskwatch records your whole message as the review; a missing or second verdict line makes the answer unacceptable and costs a round.
