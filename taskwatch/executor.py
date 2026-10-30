"""Read the verdict line of a review output.

A review output holds exactly one line that is a JSON object:
{"verdict": "PASS" or "FAIL", "blocking": true or false, "reviewer": "<name>"}.
"""

import json


def _verdict_line(body: str) -> tuple[str, bool, str]:
    found = []
    for line in body.splitlines():
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            obj = json.loads(text)
        except ValueError:
            continue
        if isinstance(obj, dict) and "verdict" in obj:
            found.append(obj)
    if len(found) != 1:
        raise ValueError("the review output needs exactly one verdict line")
    obj = found[0]
    verdict, blocking, reviewer = obj.get("verdict"), obj.get("blocking"), obj.get("reviewer")
    if verdict not in {"PASS", "FAIL"} or not isinstance(blocking, bool):
        raise ValueError("the verdict line needs verdict PASS or FAIL and a boolean blocking")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("the verdict line needs a reviewer")
    if verdict == "PASS" and blocking:
        raise ValueError("a PASS cannot carry a blocking finding")
    return verdict, blocking, reviewer.strip()
