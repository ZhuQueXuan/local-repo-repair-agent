"""Bound model-facing output while retaining complete, traceable tool results."""
import hashlib
import json
from pathlib import Path
import shlex


class ToolOutputBudget:
    def __init__(self, work: Path, *, max_output_bytes: int = 1024):
        if max_output_bytes < 512:
            raise ValueError("Output budget is too small for a traceable preview.")
        self.work = work.resolve()
        self.directory = self.work / "temp" / "tool-output"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_output_bytes = max_output_bytes
        self.events: list[dict] = []

    def process(self, output: dict, *, command: str, read_span=None) -> dict:
        original = output["output"]
        raw = original.encode("utf-8")
        number = len(self.events) + 1
        path = self.directory / f"{number:04d}.txt"
        with path.open("xb") as stream:
            stream.write(raw)
        relative = path.relative_to(self.work).as_posix()
        lines = original.lstrip().splitlines()
        submitted = bool(lines) and lines[0].strip() == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" and output["returncode"] == 0
        truncated = len(raw) > self.max_output_bytes and not submitted
        visible = original
        delivered = None
        if truncated:
            notice = (f"[TRUNCATED: full output at {relative}; {len(raw)} UTF-8 bytes. "
                      f"Read needed lines with sed -n 'START,ENDp' {relative}.]\n[HEAD]\n")
            separator = "\n[... middle omitted ...]\n[TAIL]\n"
            remaining = self.max_output_bytes - len((notice + separator).encode("utf-8"))
            head = remaining * 2 // 3
            visible = notice + raw[:head].decode("utf-8", errors="ignore") + separator + raw[-(remaining - head):].decode("utf-8", errors="ignore")
            if read_span:
                # Exact simple sed/cat only: show a contiguous complete page,
                # with an executable continuation in original source lines.
                # The unmodified full result still stays in the archive.
                name, start, requested_end = read_span
                page = []
                for index, line in enumerate(original.splitlines(keepends=True), start):
                    candidate = page + [line]
                    end = start + len(candidate) - 1
                    follow = f"sed -n '{end+1},{requested_end}p' {shlex.quote(name)}"
                    label = (f"[SOURCE PAGE {name}:{start}-{end}; remainder omitted. "
                             f"Next: {follow}. Full raw output: {relative}.]\n")
                    numbered = ''.join(f'{start+i}: {text}' for i, text in enumerate(candidate))
                    if len((label + numbered).encode('utf-8')) > self.max_output_bytes:
                        break
                    page = candidate
                    visible = label + numbered
                    delivered = [name, start, end]
        event = {"sequence": number, "command": command, "returncode": output["returncode"],
                 "exception_info": output.get("exception_info", ""), "raw_bytes": len(raw),
                 "visible_bytes": len(visible.encode("utf-8")), "truncated": truncated,
                 "full_output_path": relative, "sha256": hashlib.sha256(raw).hexdigest()}
        if delivered:
            event['delivered_source_span'] = delivered
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.events.append(event)
        return output | {"output": visible, "extra": output.get("extra", {}) | {"tool_output": event}}

    def statistics(self) -> dict:
        return {"observations": len(self.events), "truncated_observations": sum(e["truncated"] for e in self.events),
                "raw_output_bytes": sum(e["raw_bytes"] for e in self.events),
                "visible_output_bytes": sum(e["visible_bytes"] for e in self.events),
                "max_visible_output_bytes": max((e["visible_bytes"] for e in self.events), default=0),
                "per_output_budget_bytes": self.max_output_bytes}
