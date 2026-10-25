# Review quality

Use this set to check that a guideline change does not make a first-round code review worse. The scorer does not call a model, and it does not decide whether a cause matches. A person does that match. The watcher never runs this.

## Scope

This is a regression check for the first round of a code review. It does not measure:

- re-review (`K-12` to `K-14`, `C-10` to `C-13`)
- design review
- review of a document that cites sources
- security, efficiency, or compatibility

No case exercises those rules yet. A pass here says nothing about them.

## Cases

13 cases: 10 with a defect and 3 that are clean. Each case has one primary purpose, and no two cases share it. An answer may still hold more than one expected finding and more than one rule id. Findings that have the same fix are one expected finding. There is no ranking of abilities and no weighted score.

Defects:

- `stall-reset`: a defect that tests still pass
- `subfolder-adopt`: data loss on a realistic path
- `lock-wait`: an unbounded wait on a contract path
- `forced-click`: every independent cause after the first (two expected findings)
- `unsupported-claim`: a document states unverified behavior as fact
- `intent-mismatch`: the change does not do what the intent asks
- `dispatch-outside-diff`: a new value reaches an unchanged dispatch in another module
- `repo-rule`: the change breaks a rule in the repository `AGENTS.md`
- `weak-test`: the product code is right, the added test passes for a broken variant
- `severity-p2`: only a stale document remains; see the severity condition below

Clean:

- `dedupe-message`: a duplicate-string cleanup
- `dedupe-helper-intent`: a cleanup that matches its intent
- `truncated-body`: a relaxed check that the caller already covers, so a correct review has no blocking finding

The purpose and rule ids live only in `answers/`.

## Case input

Each case is `cases/<name>/input/`:

- `spec.md`
- `diff.patch` and the resulting files
- `intent.md` only when that case has one
- any other file the case needs: an unchanged module the change feeds, a test file, a document, or the repository `AGENTS.md`

If `intent.md` is absent, the assignment does not require it. Do not treat that absence as an incomplete review.

Give the reviewer a copy of `input/` in a neutral directory, so the case name is not in the path. Do not give the reviewer `answers/`, a mapping, a previous review, or this file.

## Answers

An answer in `answers/<name>.json` has:

- `purpose`: the one primary purpose of the case
- `rules`: the rule ids the case exercises
- `verdict`: `correct` or `incorrect`
- `findings`: the expected findings, each with `id`, `location`, `severity`, and a one-line `cause`. A clean case has `[]`.
- `condition`: only on a case with a case-specific pass condition
- `broken_variant`: only on `weak-test`, the change to the product code that the added test does not catch
- `version_note`: only when an answer was corrected, with the reason

## Runs

- Use the same model, effort, inputs, and tool permissions for every review. Start a fresh session for each review.
- Record the model that actually ran and any fallback. Discard a run that fell back to another model.
- A first comparison is 26 reviews: 13 cases, once per guideline set, about 1 to 2 minutes each.
- After it, run both guideline sets once more on every case where the missed count, the false positives, the severity mismatches, the severity the reviewer gave to any matched finding, or the verdict changed, and on one clean case.
- Do not claim an improvement or a pass unless the direction repeats.
- Also list the new misses and the resolved misses from the mappings. Equal totals can hide different findings.

## Matching

For each case, ask the reviewer for a verdict of `correct` or `incorrect`, and for findings with an id, a `file:line` location, a severity (`P0` to `P3`), and a one-line cause.

Map each reviewer finding to at most one expected finding, one to one. Match when the reviewer names the same root cause and the same failure path. The location only has to be valid evidence for that cause. It does not have to be the answer's line range. The same location with a different cause is not a match: leave the expected finding unmatched and list the reviewer finding as unmatched. That is one miss and one false positive. Give a one-line reason for every match and every unmatched finding.

Save the mapping next to the run, not inside `input/`. One file per case, named `<name>.json`. Copy the reviewer's verdict and severity. Do not correct them. See [mapping.example.json](mapping.example.json).

## Scoring

```sh
uv run python scripts/score_reviews.py --answers evals/answers --mappings <run>/mappings
```

The script prints, per case and in total:

- `missed`: expected findings with no match
- `false_positives`: reviewer findings with no match. On a clean case every reviewer finding is one.
- `severity_mismatches`: matched findings whose reviewer severity differs from the expected one. A miss is not also a mismatch.
- `verdict_agreements`: how many verdicts equal the answer. This is reported and does not replace the counts above.

A new guideline passes when it has no more missed findings, no more false positives, and no more severity mismatches than the old one.

Severity condition: `severity-p2` passes only when its expected finding is matched at exactly `P2` and the reviewer's verdict is `correct`. The script reports `condition.passed` for that case and `failed_conditions` in the totals. This is in addition to the general rule, not a score.

An empty-guideline run is optional. It only shows what the guideline adds. A gap is not a pass condition. Do not change the cases to create one.

## Maintenance

- When a reviewer misses a real defect, add a case for it. Fix its answer from evidence before any guideline change, then compare again.
- Never edit a case to make a guideline pass. A wrong answer may be corrected, with a `version_note` that gives the reason.
- Results on cases that were used to write a guideline are regression results. They are not evidence that the guideline generalizes.
