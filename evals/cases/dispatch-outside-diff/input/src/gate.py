"""Decide whether a set of check results lets a change through."""

from src.checks import Result


def decide(results: list[Result]) -> str:
    passed: list[str] = []
    failed: list[str] = []
    for result in results:
        if result.status == "pass":
            passed.append(result.name)
        elif result.status == "fail":
            failed.append(result.name)
    if failed:
        return "blocked"
    return "clear"
