import sys

import pytest

from taskwatch.job_rpc import RpcClient, RpcError

PEER = r"""
import base64, json, sys
out = lambda f: print(json.dumps(f), flush=True)
out({"type": "ready", "protocolVersion": 1, "supportedProtocolVersions": [1, 2]})
for line in sys.stdin:
    cmd = json.loads(line)
    out({"type": "response", "id": cmd["id"], "command": cmd["type"], "success": True, "data": {}})
part = base64.b64encode(b"{").decode()
out({"type": "rpc_chunk", "chunkId": "cut", "index": 0, "count": 2, "byteLength": 40, "data": part})
"""


def test_chunk_sequence_cut_off_by_peer_shutdown_fails_the_step(tmp_path):
    with RpcClient([sys.executable, "-c", PEER], tmp_path) as rpc:
        rpc.start(5)
        with pytest.raises(RpcError, match="interrupted"):
            rpc.finish()
