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
```

CI runs the same commands. A pull request must pass all of them.

## Code style

- Python 3.11 or later, no runtime dependencies.
- Checks are pure functions. Config and state are frozen dataclasses. No inheritance. Only board access is a `Protocol`, so tests can use a fake board.
- Keep changes minimal and focused. One function does one thing.
- Tests must fail when the behavior they cover breaks.
- Comments explain why, not what.
- Ruff rules `E`, `F`, `I`, `C901` (max complexity 15), line length 100.

## Commits and pull requests

- Commit messages and PR titles use [Conventional Commits](https://www.conventionalcommits.org/): `type(scope): summary`, in English, with no trailers.
- Pull requests are squash merged.
- Fill in the pull request template. Link the issue with `Closes #`.
- Add one line under `Unreleased` in [CHANGELOG.md](CHANGELOG.md) when the change is visible to users.
- Do not commit secrets, personal values, or local paths. Use placeholders in examples.

## Using AI tools

AI tools are welcome. You are responsible for what you submit:

- Check the result and attach evidence (test output, logs, or the review that found no issues).
- Make sure you can explain the change and maintain it.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
