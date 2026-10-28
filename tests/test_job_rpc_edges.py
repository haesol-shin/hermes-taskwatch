import json
import sys

import pytest

from taskwatch.job_rpc import MissingTranscript, RpcClient, RpcError

PEER = r"""
import base64, json, os, sys
mode = os.environ["PEER_MODE"]
child = json.loads(os.environ.get("PEER_CHILD", "{}"))
out = lambda f: print(json.dumps(f), flush=True)
out({"type": "ready", "protocolVersion": 1, "supportedProtocolVersions": [1, 2]})
for line in sys.stdin:
    cmd = json.loads(line)
    data, ok = {}, True
    if cmd["type"] == "get_subagents":
        data = {"subagents": [child] if child else []}
    elif cmd["type"] == "get_subagent_messages":
        if mode == "evicted":
            ok = False
        else:
            data = {"messages": [], "nextByte": 0, "reset": False}
    out({"type": "response", "id": cmd["id"], "command": cmd["type"], "success": ok, "data": data,
         "error": "unknown subagent"})
if mode == "cut":
    part = base64.b64encode(b"{").decode()
    out({"type": "rpc_chunk", "chunkId": "cut", "index": 0, "count": 2, "byteLength": 40,
         "data": part})
"""


def client(tmp_path, monkeypatch, mode: str, child: dict | None = None) -> RpcClient:
    monkeypatch.setenv("PEER_MODE", mode)
    monkeypatch.setenv("PEER_CHILD", json.dumps(child or {}))
    return RpcClient([sys.executable, "-c", PEER], tmp_path)


def test_chunk_sequence_cut_off_by_peer_shutdown_fails_the_step(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch, "cut") as rpc:
        rpc.start(5)
        with pytest.raises(RpcError, match="interrupted"):
            rpc.finish()


def test_completed_child_with_empty_transcript_is_missing_evidence(tmp_path, monkeypatch):
    kid = {"id": "Kid", "status": "completed"}
    with client(tmp_path, monkeypatch, "empty", kid) as rpc:
        rpc.start(5)
        with pytest.raises(MissingTranscript, match="Kid"):
            rpc.children(5)


def test_running_child_may_have_no_transcript_yet(tmp_path, monkeypatch):
    kid = {"id": "Kid", "status": "running"}
    with client(tmp_path, monkeypatch, "empty", kid) as rpc:
        rpc.start(5)
        assert rpc.children(5) == [{**kid, "messages": []}]


def test_cached_terminal_transcript_survives_server_eviction(tmp_path, monkeypatch):
    kid = {"id": "Kid", "status": "completed", "sessionFile": "k.jsonl"}
    saved = {**kid, "messages": [{"role": "assistant"}]}
    with client(tmp_path, monkeypatch, "evicted", kid) as rpc:
        rpc.start(5)
        assert rpc.children(5, {"Kid\0k.jsonl": saved}) == [saved]


def test_cache_is_not_used_for_other_execution_or_empty_record(tmp_path, monkeypatch):
    kid = {"id": "Kid", "status": "completed", "sessionFile": "new.jsonl"}
    other = {**kid, "sessionFile": "old.jsonl", "messages": [{"role": "assistant"}]}
    empty = {**kid, "messages": []}
    with client(tmp_path, monkeypatch, "evicted", kid) as rpc:
        rpc.start(5)
        with pytest.raises(MissingTranscript, match="unknown subagent"):
            rpc.children(5, {"Kid\0old.jsonl": other})
        with pytest.raises(MissingTranscript, match="unknown subagent"):
            rpc.children(5, {"Kid\0new.jsonl": empty})


def test_cache_does_not_hide_a_still_running_child(tmp_path, monkeypatch):
    kid = {"id": "Kid", "status": "running", "sessionFile": "k.jsonl"}
    saved = {**kid, "status": "completed", "messages": [{"role": "assistant"}]}
    with client(tmp_path, monkeypatch, "evicted", kid) as rpc:
        rpc.start(5)
        with pytest.raises(MissingTranscript, match="unknown subagent"):
            rpc.children(5, {"Kid\0k.jsonl": saved})
