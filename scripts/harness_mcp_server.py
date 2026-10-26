"""One bounded stdio MCP tool. Input is supplied patch bytes, never host paths.

Implements the 2025-06-18 lifecycle/tools subset using only the standard library.
This trusted built-in process does not offer resources, sampling or execution.
"""
import hashlib
import json
import re
import sys

PROTOCOL = "2025-06-18"
MAX_FRAME = 1048576
MAX_RESPONSE = 16384
TOOL = {"name": "change_overview", "description": "Count changed lines by path in the supplied frozen unified diff. Navigation only; not defect or test evidence.",
    "inputSchema": {"type": "object", "properties": {"candidate_id": {"type": "string"},
        "source_tree_digest": {"type": "string"}, "patch_sha256": {"type": "string"}, "patch": {"type": "string"}},
        "required": ["candidate_id", "source_tree_digest", "patch_sha256", "patch"], "additionalProperties": False},
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}}


def overview(arguments):
    if not isinstance(arguments, dict) or set(arguments) != set(TOOL["inputSchema"]["required"]):
        raise ValueError("invalid_arguments")
    if any(not isinstance(v, str) for v in arguments.values()):
        raise ValueError("invalid_argument_types")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", arguments["candidate_id"]) or any(
            not re.fullmatch(r"[a-f0-9]{64}", arguments[k]) for k in ("source_tree_digest", "patch_sha256")):
        raise ValueError("invalid_version_bindings")
    raw = arguments["patch"].encode("utf-8")
    if len(raw) > 524288 or hashlib.sha256(raw).hexdigest() != arguments["patch_sha256"]:
        raise ValueError("patch_digest_or_size_mismatch")
    rows = []; current = None; additions = deletions = old_left = new_left = 0
    pending_header = False
    for line in arguments["patch"].splitlines():
        if old_left or new_left:
            if line.startswith("+"):
                current["added"] += 1; additions += 1; new_left -= 1
            elif line.startswith("-"):
                current["removed"] += 1; deletions += 1; old_left -= 1
            elif line.startswith(" "):old_left -= 1; new_left -= 1
            elif not line.startswith("\\"):raise ValueError("invalid_diff_hunk_body")
            if old_left < 0 or new_left < 0:raise ValueError("invalid_diff_hunk_counts")
        elif line.startswith("--- a/"):
            pending_header = True
        elif line.startswith("+++ b/") and pending_header:
            path = line[6:]
            if len(path) > 240 or path.startswith(("/", "\\")) or ".." in path.split("/"):
                raise ValueError("invalid_diff_path")
            current = {"path": path, "added": 0, "removed": 0, "hunks": 0}
            rows.append(current)
            pending_header = False
        elif line.startswith("@@ ") and current is not None:
            match = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*", line)
            if match is None:raise ValueError("invalid_diff_hunk_header")
            old_left, new_left = int(match[2] or 1), int(match[4] or 1)
            current["hunks"] += 1
    if old_left or new_left or pending_header:raise ValueError("incomplete_diff")
    return {"candidate_id": arguments["candidate_id"], "source_tree_digest": arguments["source_tree_digest"],
        "patch_sha256": arguments["patch_sha256"], "changed_files": len(rows), "added": additions, "removed": deletions,
        "files": rows[:20], "truncated": len(rows) > 20,
        "scope": "Navigation over this diff only. Counts do not prove correctness, coverage or successful tests. Use original evidence tools for citations."}


def serve():
    initialized = ready = False
    for _ in range(32):
        raw = sys.stdin.buffer.readline(MAX_FRAME + 1)
        if not raw:return
        if len(raw) > MAX_FRAME or not raw.endswith(b"\n"):return
        request_id = None
        try:
            request = json.loads(raw)
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                raise ValueError("invalid_request")
            request_id = request.get("id")
            method = request.get("method"); params = request.get("params", {})
            if request_id is None:
                if method == "notifications/initialized" and initialized:ready = True
                continue
            if type(request_id) not in (str, int):raise ValueError("invalid_request_id")
            if method == "initialize" and not initialized:
                if params.get("protocolVersion") != PROTOCOL:raise ValueError("unsupported_protocol")
                initialized = True
                result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "local-repair-change-overview", "version": "1.0.0"}}
            elif ready and method == "ping":result = {}
            elif ready and method == "tools/list":result = {"tools": [TOOL]}
            elif ready and method == "tools/call":
                if params.get("name") != TOOL["name"]:raise ValueError("unknown_tool")
                try:
                    content = overview(params.get("arguments"))
                    result = {"content": [{"type": "text", "text": json.dumps(content, ensure_ascii=False, separators=(",", ":"))}],
                              "structuredContent": content, "isError": False}
                except ValueError as error:
                    result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
            else:
                response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not supported in this bounded MCP server"}}
                result = None
            if result is not None:response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (ValueError, TypeError, AttributeError):
            response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "Invalid bounded MCP request"}}
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()
        if len(encoded) > MAX_RESPONSE:return
        sys.stdout.buffer.write(encoded + b"\n");sys.stdout.buffer.flush()


if __name__ == "__main__":serve()
