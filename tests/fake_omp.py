"""A fake `omp --mode rpc` for tests. Synthetic: no model, no network.

FAKE_OMP_SCENARIO is a JSON file: {"chunk_at": int|null, "turns": [turn, ...]}.
FAKE_OMP_STATE is a directory that holds the shared turn counter and log.jsonl, which
records every command received so a test can assert what the runner sent.

A turn runs when a prompt arrives (the Nth prompt over all processes takes turn N):
  writes: {path: text}      files written relative to the working directory
  run: [argv, ...]          commands run in the working directory
  subagents: [{id, status, messages}]  lifecycle frames; only running ones stay listed
  die_after_children: "read" exits once a child transcript was fully read; "now" at once
  followup_error: stopReason of an async follow-up run that ends after prompt_result
  reply: str                the final assistant text
  die: true                 exit after agent_start
  no_agent_start, no_settle: skip that frame / never settle
  status: prompt_result status (default completed)
  sleep: seconds to wait before finishing the turn
  notice: emit a session-persistence error notice; oversize: emit a frame over the limit
  (scenario) exit_code: exit code after stdin closes
"""

import base64
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

SCENARIO = json.loads(Path(os.environ["FAKE_OMP_SCENARIO"]).read_text(encoding="utf-8"))
STATE = Path(os.environ["FAKE_OMP_STATE"])
STATE.mkdir(parents=True, exist_ok=True)
FRAME_LIMIT = 1 << 20

protocol = 1
chunks = 0
session_file: Path | None = None
settled = True
reply = ""
subagents: list[dict] = []
LOCK = threading.RLock()  # one frame, or one chunk sequence, at a time
children_read = threading.Event()
turns: list[threading.Thread] = []


def emit(frame: dict) -> None:
    with LOCK:
        _emit(frame)


def _emit(frame: dict) -> None:
    global chunks
    line = json.dumps(frame)
    limit = SCENARIO.get("chunk_at")
    if protocol == 2 and limit and len(line) + 1 > limit:
        data = line.encode("utf-8")
        size = max(1, limit // 2)
        parts = [data[i : i + size] for i in range(0, len(data), size)]
        chunks += 1
        for index, part in enumerate(parts):
            sys.stdout.write(
                json.dumps(
                    {
                        "type": "rpc_chunk",
                        "chunkId": f"rpc-{chunks}",
                        "index": index,
                        "count": len(parts),
                        "byteLength": len(data),
                        "data": base64.b64encode(part).decode("ascii"),
                    }
                )
                + "\n"
            )
    else:
        sys.stdout.write(line + "\n")
    sys.stdout.flush()


def log(entry: dict) -> None:
    with (STATE / "log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def next_turn() -> dict:
    counter = STATE / "turn.txt"
    n = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(n + 1))
    turns = SCENARIO.get("turns", [])
    if n >= len(turns):
        raise RuntimeError("fake RPC scenario has no remaining turn")
    return turns[n]


def respond(cmd: dict, data=None, ok: bool = True, error: str = "") -> None:
    frame = {"id": cmd.get("id"), "type": "response", "command": cmd["type"], "success": ok}
    if ok and data is not None:
        frame["data"] = data
    if not ok:
        frame["error"] = error
    emit(frame)


def open_session(cmd: dict) -> None:
    global session_file
    directory = Path(cmd["sessionDir"])
    directory.mkdir(parents=True, exist_ok=True)
    existing = sorted(directory.glob("*.jsonl"))
    resumed = bool(existing)
    session_file = existing[-1] if existing else directory / "s1.jsonl"
    session_file.touch()
    respond(
        cmd,
        {
            "cancelled": False,
            "resumed": resumed,
            "sessionId": "sid",
            "sessionFile": str(session_file),
        },
    )


def prompt(cmd: dict) -> None:
    global settled, reply
    turn = next_turn()
    settled = False
    respond(cmd, {})
    if not turn.get("no_agent_start"):
        emit({"type": "agent_start"})
    if turn.get("die"):
        os._exit(3)
    time.sleep(turn.get("sleep", 0))
    for rel, text in turn.get("writes", {}).items():
        path = Path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for argv in turn.get("run", []):
        subprocess.run(argv, check=True, capture_output=True)
    for child in turn.get("subagents", []):
        subagents.append(child)
        payload = {"id": child["id"], "agent": "task", "index": 0, "sessionFile": "child.jsonl"}
        emit({"type": "subagent_lifecycle", "payload": {**payload, "status": "started"}})
        if child["status"] != "running":
            emit({"type": "subagent_lifecycle", "payload": {**payload, "status": child["status"]}})
    after = turn.get("die_after_children")
    if after == "read":
        # Die only once the client has read a child transcript: no timing involved.
        children_read.wait(10)
        os._exit(3)
    if after == "now":
        os._exit(3)
    reply = turn.get("reply", "")
    if turn.get("notice"):
        emit({"type": "notice", "level": "error", "source": "session-persistence", "message": "x"})
    if turn.get("oversize"):
        big = {"type": "message_update", "pad": "x" * (FRAME_LIMIT + 10)}
        with LOCK:
            sys.stdout.write(json.dumps(big) + "\n")
            sys.stdout.flush()
    if session_file is not None:
        with session_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"prompt": cmd["message"]}) + "\n")
    emit({"type": "agent_end", "messages": [], "isTerminal": True, "yielded": True})
    followup = turn.get("followup_error")
    emit(
        {
            "type": "prompt_result",
            "id": cmd["id"],
            "agentInvoked": True,
            "status": turn.get("status", "completed"),
            "sessionSettled": not (turn.get("no_settle") or followup),
        }
    )
    if followup:
        # An async follow-up run fails after the one prompt_result; no second one is sent.
        emit({"type": "agent_start"})
        message = {"role": "assistant", "stopReason": followup, "content": []}
        emit({"type": "message_end", "message": message})
        emit({"type": "agent_end", "messages": [message], "isTerminal": True, "yielded": True})
    if not turn.get("no_settle"):
        settled = True
        emit({"type": "session_settled"})


def subagent_messages(cmd: dict) -> None:
    child = next((c for c in subagents if c["id"] == cmd.get("subagentId")), None)
    offset = int(cmd.get("fromByte", 0))
    messages = child["messages"] if child else []
    page = messages[offset : offset + 2]
    respond(
        cmd,
        {
            "sessionFile": "",
            "fromByte": offset,
            "nextByte": offset + len(page),
            "reset": False,
            "entries": page,
            "messages": page,
        },
    )
    if child and offset >= len(messages):
        children_read.set()


def handle(cmd: dict) -> None:
    global protocol
    log(cmd)
    kind = cmd["type"]
    if kind == "negotiate_protocol":
        protocol = cmd["protocolVersion"]
        respond(cmd, {"protocolVersion": protocol})
    elif kind == "open_session":
        open_session(cmd)
    elif kind == "set_subagent_subscription":
        respond(cmd, {"level": cmd["level"]})
    elif kind == "get_state":
        respond(cmd, {"isSettled": settled, "hasPendingAsyncWork": False})
    elif kind == "prompt":
        # A real session keeps serving commands while a turn runs.
        thread = threading.Thread(target=prompt, args=(cmd,))
        turns.append(thread)
        thread.start()
    elif kind == "get_subagents":
        # Terminal subagents are pruned from the registry; their transcripts stay selectable.
        listed = [
            {k: v for k, v in c.items() if k != "messages"}
            for c in subagents
            if c["status"] == "running"
        ]
        respond(cmd, {"subagents": listed})
    elif kind == "get_subagent_messages":
        subagent_messages(cmd)
    elif kind == "get_last_assistant_text":
        respond(cmd, {"text": reply})
    elif kind == "abort":
        pass
    else:
        respond(cmd, ok=False, error=f"unknown command {kind}")


def main() -> None:
    log({"argv": sys.argv[1:], "cwd": os.getcwd()})
    emit(
        {
            "type": "ready",
            "protocolVersion": 1,
            "supportedProtocolVersions": [1, 2],
            "maxFrameBytes": FRAME_LIMIT,
            "maxReassembledFrameBytes": 64 << 20,
        }
    )
    for raw in sys.stdin:
        if raw.strip():
            handle(json.loads(raw))
    for thread in turns:
        thread.join()
    sys.exit(SCENARIO.get("exit_code", 0))


if __name__ == "__main__":
    main()
