# AGENTS.md

Instructions for coding agents working in this repository.

## Commands

```sh
uv sync --locked
uv run ruff check
uv run ruff format --check
uv run pytest
uv run python scripts/check_review_docs.py
```

Run all four before you report a change as done.

## Rules

- Read [CONTRIBUTING.md](CONTRIBUTING.md) first. It is the source for code style, commits, and pull requests.
- No runtime dependencies. Standard library only.
- Checks never call a model.
- Do not import Hermes in check code. Board access goes through the `Protocol` in the package, so tests use a fake board.
- Keep the change minimal. Do not refactor unrelated code.
- Tests use synthetic fixtures. Use `.invalid` hosts and placeholder paths, never real accounts, chat ids, or local paths.

## Before you publish

- Ask the maintainer before you post an issue, a comment, or a pull request, and before you merge.
- Search your diff for personal values, tokens, and absolute local paths.
- Add a `CHANGELOG.md` line under `Unreleased` if users can see the change.
