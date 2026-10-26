"""A stand-in executor. Its brief is a JSON file that says what to do."""

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--brief", required=True)
    parser.add_argument("--copy", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    plan = json.loads(Path(args.brief).read_text(encoding="utf-8"))
    print(plan.get("print", ""))
    for path, text in plan.get("write", {}).items():
        Path(path).write_text(text, encoding="utf-8")
    for name, text in plan.get("write_in_copy", {}).items():
        (Path(args.copy) / name).write_text(text, encoding="utf-8")
    if "echo_copy" in plan:
        Path(plan["echo_copy"]).write_text(args.copy, encoding="utf-8")
    if "output" in plan:
        Path(args.output).write_text(plan["output"], encoding="utf-8")
    sys.exit(plan.get("exit", 0))


if __name__ == "__main__":
    main()
