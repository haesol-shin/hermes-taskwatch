# Contributing

Thank you for your interest in hermes-taskwatch. The project is experimental, so the rules are short.

## Before you start

- For a bug, open an issue with the bug form.
- For a new feature or a larger change, open an issue with the feature form first and agree on the approach before you write code.
- Small fixes (typos, documentation, obvious bugs) can go straight to a pull request.

## Development

The project uses [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked
uv run ruff check
uv run ruff format --check
uv run pytest
uv run python scripts/check_review_docs.py
```

CI runs the same commands. A pull request must pass all of them.

## Code style

- Python 3.11 or later, no runtime dependencies.
- Checks are pure functions. Config and state are frozen dataclasses. No inheritance. Only board access is a `Protocol`, so tests can use a fake board.
- Keep changes minimal and focused. One function does one thing.
- Tests must fail when the behavior they cover breaks.
- Comments explain why, not what.
- Ruff rules `E`, `F`, `I`, `C901` (max complexity 15), line length 100.

## Commits

A commit message explains why the code is the way it is. The diff already shows what changed, so write only what the diff cannot tell a future reader.

- Title: [Conventional Commits](https://www.conventionalcommits.org/), `type(scope): summary` in the imperative, in English, 72 characters or fewer (about 50 is best). No trailers.
- Body: up to three bullets, each one line of 72 characters or fewer:
  1. The problem or the reason for the change.
  2. The new behavior, described as behavior, not as a file list.
  3. A rejected alternative, only when another approach looks more natural (`Rejected: ...`).
- Do not list files, restate the diff, describe the review process, or paste test results. Test results go in the pull request.

Example:

```
fix(gate): block completion when the review is for an old commit

- A pass on an earlier commit let a changed result complete
- The gate now compares the reviewed SHA with the current HEAD
- Rejected: warn only, because the card would already move on
```

## Pull requests

Pull request titles use the same `type(scope): summary` format as commit titles.

The pull request body is the evidence for merging this change now: tests run, CI, review, linked issue, and known limits. It stays on GitHub and is reached through the `#number` in the commit title.

- Fill in the pull request template. Link the issue with `Closes #`.
- Add one line under `Unreleased` in [CHANGELOG.md](CHANGELOG.md) when the change is visible to users.
- Do not commit secrets, personal values, or local paths. Use placeholders in examples.
- Merging:
  - Every pull request is squash merged, so `main` has one commit per pull request. Write the squash subject as the pull request title plus `(#number)` and the body by the commit rules above, replacing GitHub's commit list. Merge commits and rebase merging are off: a pull request is one change, and its step commits stay on the pull request page.
  - Split a large change into pull requests that can each be reviewed and merged on their own ([Google, Small CLs](https://google.github.io/eng-practices/review/developer/small-cls.html)).
  - Merge only after the maintainer approves the current head, and pin it: `gh pr merge <number> --match-head-commit <approved SHA>`. A new commit after approval needs a new approval.

## Using AI tools

AI tools are welcome. You are responsible for what you submit:

- Check the result and attach evidence (test output, logs, or the review that found no issues).
- Make sure you can explain the change and maintain it.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
