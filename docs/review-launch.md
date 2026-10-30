# Launching a review

One set of review guidelines, [docs/review](review/README.md), applies however a review starts. Three things differ by launch path and are kept out of that folder:

| Layer | Where | What it holds |
| --- | --- | --- |
| Guidelines | `docs/review/` | What makes a result pass: rules, severity, rounds, incomplete reviews. Same for every path. |
| Launch layer | `docs/launch/taskwatch.md`, `docs/launch/omp-alone.md` | Verdict format, who counts rounds, tool restrictions, where inputs come from. |
| Agent definition | an omp agent file, for example [examples/omp/reviewer.md](../examples/omp/reviewer.md) | Who the reviewer is: tools, allowed sub-agents, model, and a short static procedure. |

The launcher, `python -m taskwatch.reviewer_launch`, joins the agent definition and the guideline folder when the review starts. It copies no rule text, so editing a guideline file changes the next review.

## What the launcher builds

From the agent file's front matter:

- `tools` is required. It becomes `--tools`; `tools: []` becomes `--no-tools`, because omitting the option would give omp's default tools, `edit` and `write` included. `task` is added when the agent may spawn anything, because spawning needs it.
- `model` becomes `--model` (the first entry of a list); `thinking-level` becomes `--thinking`.
- `spawns` is the list of sub-agents the review may use. Every other agent the launcher can find (bundled, user, and the project agent folder of every directory from the working directory up) is disabled in an overlay, because omp enforces that list and a prompt does not. `spawns: "*"` disables none. Name agents the folders do not show, such as plugin agents, with `--disable-agent <name>` (repeatable). `tools`, `spawns`, `model` and `thinking-level` may be inline lists or block lists; a value the launcher cannot read stops the launch instead of being dropped.

The agent body, then every guideline file named by `--kind` (the README and common rules, then the kind's file; all kinds when `--kind` is omitted), then the optional `--layer` file become one file passed as `--append-system-prompt`. `autoloadSkills` and the nested `output` schema are ignored: omp does not apply an agent's output schema to a command-line session, so the verdict format comes from the launch layer.

The overlay also sets `async.enabled: false`. With async on, `omp -p` ends before a background sub-agent reports, and the review has no verdict.

The two files are written under a name that holds their content hash (default `$TMPDIR/taskwatch-reviewer/`; `--work-dir` moves it), so a rerun with unchanged sources reuses them. Options the launcher does not know pass through to omp *before* its own options, so the overlay it builds is the last one omp reads and a pass-through option cannot widen the review. `--no-extensions` is always set, and pass-through `--tools`, `--no-tools`, `--config`, `--append-system-prompt`, `--system-prompt`, `-e`, `--hook`, `--plugin-dir` and `--trusted-extension` are refused.

Known limit: omp has no setting that restricts the tools of a command-line session to the list. Tools from MCP servers or custom-tool folders that omp loads on its own are not removed by `--tools`, and agents from plugin roots are removed only when named. Use a profile without them for a review that must be read-only by construction.

## Through taskwatch

Set the review step's command in `jobs-config.json`:

```json
"review_command": [
  "python3", "-m", "taskwatch.reviewer_launch",
  "--omp", "/usr/local/bin/omp",
  "--agent", "/srv/example/reviewer.md",
  "--guidelines", "{guidelines}", "--kind", "{kind}"
]
```

`{guidelines}` is the job's own copy of the guidelines, taken when the job first ran, so the basis of the review cannot change under a job. A new job reads the current files. The runner adds `--mode rpc --no-ui`. The message it sends holds the intent, the spec, the target, the round, and `docs/launch/taskwatch.md`.

Submit a [review-only job](configuration.md#review-only-jobs) to review something that already exists.

## With omp alone

Run the same launcher by hand, with the other launch layer:

```sh
python3 -m taskwatch.reviewer_launch \
  --agent examples/omp/reviewer.md \
  --guidelines docs/review --kind code \
  --layer docs/launch/omp-alone.md \
  -p "Round 1. Review commit <sha> of the repository in the current directory against the intent in intent.md and the spec in spec.md."
```

`--print-command` prints the `omp` command instead of running it, so you can read it or run `omp` yourself. `--guidelines` defaults to the folder installed with the package.

Both paths receive the same guideline text. They differ only in the launch layer and in the message.
