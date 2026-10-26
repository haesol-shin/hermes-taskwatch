# AGENTS.md

Instructions for coding agents working in this repository.

## Rules

- A check is a function of the data it is given. It never opens a connection and never reads a file.
- Board data reaches a check through the `Board` protocol in `src/board.py`.
- Keep the change minimal. Do not refactor unrelated code.
