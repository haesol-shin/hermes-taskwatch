"""Run one named check and return its result."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Result:
    name: str
    status: str


def run_check(name: str, check) -> Result:
    try:
        ok = check()
    except TimeoutError:
        return Result(name, "timeout")
    return Result(name, "pass" if ok else "fail")
