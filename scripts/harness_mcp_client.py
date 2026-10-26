"""Fixed local MCP server, bounded frames/deadline, no caller-supplied commands."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from collaboration_protocol import ProtocolError, canonical_bytes, sha256
from harness_mcp_server import MAX_FRAME, MAX_RESPONSE, PROTOCOL, TOOL

ROOT = Path(__file__).resolve().parent.parent


def change_overview(arguments, check, deadline, *, expected_server_sha256=None):
    check()
    from review_tools import _read_bytes
    source = _read_bytes(ROOT, "scripts/harness_mcp_server.py")
    source_sha256 = sha256(source)
    if expected_server_sha256 is not None and source_sha256 != expected_server_sha256:
        raise ProtocolError("mcp_server_source_changed")
    expires = min(deadline, time.monotonic() + 8)
    env = {k: os.environ[k] for k in ("SYSTEMROOT", "SystemRoot", "WINDIR") if k in os.environ}
    env.update(TEMP=str(ROOT / "temp"), TMP=str(ROOT / "temp"))
    process = subprocess.Popen([sys.executable, "-I", "-B", "-c", source.decode("utf-8")],
        cwd=ROOT / "temp", env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    incoming = queue.Queue(maxsize=5)
    def read_frames():
        try:
            for _ in range(5):
                raw = process.stdout.readline(MAX_RESPONSE + 1)
                incoming.put_nowait(raw)
                if not raw or len(raw) > MAX_RESPONSE:break
        except (OSError, ValueError, queue.Full):
            pass
    thread = threading.Thread(target=read_frames, daemon=True)
    thread.start()
    trace = []
    transport = {"protocol": PROTOCOL, "transport": "stdio", "pid": process.pid,
        "exchanges": trace, "server_source_sha256": source_sha256, "inherited_environment_keys": sorted(env),
        "tool_definition_sha256": sha256(canonical_bytes(TOOL))}
    def send(value):
        check()
        raw = canonical_bytes(value)
        if len(raw) + 1 > MAX_FRAME:raise ProtocolError("mcp_request_frame_too_large")
        process.stdin.write(raw + b"\n");process.stdin.flush()
    def request(ordinal, method, params):
        frame = {"jsonrpc": "2.0", "id": ordinal, "method": method, "params": params}
        send(frame)
        while True:
            check()
            if time.monotonic() >= expires:raise TimeoutError("Bounded MCP tool timed out")
            try:raw = incoming.get(timeout=min(.05, max(.001, expires - time.monotonic())))
            except queue.Empty:continue
            if not raw or len(raw) > MAX_RESPONSE or not raw.endswith(b"\n"):
                raise ProtocolError("mcp_invalid_response_frame")
            try:response = json.loads(raw)
            except ValueError:raise ProtocolError("mcp_response_not_json") from None
            if not isinstance(response, dict) or response.get("id") != ordinal or response.get("jsonrpc") != "2.0" or "error" in response:
                raise ProtocolError("mcp_response_identity_or_error")
            result = response.get("result")
            if not isinstance(result, dict):raise ProtocolError("mcp_result_not_object")
            trace.append({"method": method, "request_sha256": sha256(canonical_bytes(frame)),
                          "response_sha256": sha256(raw.rstrip(b"\n")), "response_bytes": len(raw)})
            return result
    try:
        hello = request(1, "initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                         "clientInfo": {"name": "local-repair-harness", "version": "1.0.0"}})
        if hello.get("protocolVersion") != PROTOCOL or hello.get("capabilities") != {"tools": {"listChanged": False}}:
            raise ProtocolError("mcp_capability_negotiation_mismatch")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = request(2, "tools/list", {})
        if listed != {"tools": [TOOL]}:raise ProtocolError("mcp_pinned_tool_definition_mismatch")
        result = request(3, "tools/call", {"name": "change_overview", "arguments": arguments})
        body = result.get("structuredContent")
        if result.get("isError") is not False or not isinstance(body, dict):
            raise ProtocolError("mcp_tool_result_rejected")
        if result.get("content") != [{"type": "text", "text": json.dumps(body, ensure_ascii=False, separators=(",", ":"))}]:
            raise ProtocolError("mcp_content_binding_mismatch")
        if any(body.get(k) != arguments[k] for k in ("candidate_id", "source_tree_digest", "patch_sha256")):
            raise ProtocolError("mcp_result_version_mismatch")
        check()
        transport["server"] = hello["serverInfo"]
        return body, transport
    finally:
        if process.stdin:process.stdin.close()
        try:process.wait(timeout=.3)
        except subprocess.TimeoutExpired:
            process.kill();process.wait(timeout=1)
        if process.stdout:process.stdout.close()
        thread.join(timeout=.2)
        transport["child_exit_code"] = process.returncode
