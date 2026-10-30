# Common review rules

Use this file for every review, then the review file the assignment names.

## Precedence

**C-1.** The requester's approval and the task spec override this folder. These rules do not override a decision the requester already made. If the request contradicts a cited decision, report that conflict.

## Purpose

**C-2.** Pass only a result the requester would accept. Judge it against why the work exists, not only against the wording of the request.

## Intent and spec

**C-3.** Judge the result against the intent and the spec. A result that matches the spec and misses the intent is a finding. A result that matches the intent and misses a spec requirement is a finding.

**C-4.** When the inputs include prior corrections from the requester, check every item. A repeat of a prior correction is blocking.

## Evidence

**C-5.** Every finding cites a location: `file:line`, a quote, or a source section. Mark it verified (you checked it) or unverified (plausible, not checked). Do not report a guess as a fact.

**C-6.** If the verdict depends on a list of findings, include every finding. A summary that points at a list you did not write is an incomplete review.

## Grouping

**C-7.** Report one finding per cause. Problems with the same fix go in one finding that lists every location. Locations that need different fixes stay separate.

## Severity

**C-8.** Give each finding one severity:

- P0: breaks a stated rule, contract, or safety property about runtime behavior or data; corrupts data; or takes an external action the assignment does not allow.
- P1: wrong on a realistic path, or does not do what the intent asks.
- P2: an edge case, a misleading document or message, a missing process artifact, or a style or wording rule break.
- P3: an improvement, not a defect.

P0 and P1 block. P2 and P3 do not. A rule break is P0 only when it is about runtime behavior or data. Style, wording, and a missing process artifact are P2 and do not block on their own. The verdict is incorrect while any P0 or P1 remains. Otherwise it is correct.

## Categories

**C-9.** Give each finding one category:

- `intent`: does not match the request, the spec, or a requester decision
- `evidence`: a claim the code, tests, or sources do not support
- `behavior`: a wrong result, or a failure on a realistic path
- `clarity`: a reader cannot understand or explain it
- `other`

## Rounds

**C-10.** Round 1 reviews the whole deliverable. A later round reviews the changes and the prior open findings, not settled ground. Do not add a requirement that does not resolve a prior finding. The assignment gives the round and, from round 2, the prior reviewed result and the prior findings; if it does not, say so.

**C-11.** Do not reopen a passed item, and do not raise its severity, unless you have evidence that the earlier verdict was wrong. Say what that evidence is. A resolved finding stays closed.

**C-12.** The assignment states the round cap. After that cap, list leftover blockers as unresolved. Do not report them as passed.

**C-13.** If the deliverable was edited outside the claimed change list, review the whole deliverable against the sources again, not only the claimed changes.

## Incomplete review

**C-14.** If a required input is missing or unreadable, or this folder did not arrive, say the review is incomplete and list what is missing. Never report an incomplete review as a pass.

**C-15.** Do not edit, commit, push, publish, or comment on the deliverable, and do not run a write mode of a command on real data. Read-only inspection is allowed when the assignment permits it. You may run existing tests and read-only commands to confirm or reproduce a finding; run a command that could write files in a temporary copy. An unchecked test run is not a pass and not, by itself, a blocker.

## Repository conventions

**C-16.** When the change is in a repository, read its `AGENTS.md` and the convention files it names. Report a violation as `intent`. Check that new or edited docs, commits, and review text follow those rules and the format of the files they touch.

## Finding content

**C-17.** A finding states the problem, what triggers it, its impact, whether the evidence is verified or unverified, and the fix, in a neutral tone. Offer a replacement only when it is concrete code or text that can be applied as written.

## Sources

Generalized from a shared review checklist and a deliverable-review role. Provenance is in the [folder README](README.md).
