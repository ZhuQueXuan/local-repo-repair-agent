"""Reversible request projection: archive references replace older tool bodies."""
import json
from pathlib import Path


def message_bytes(messages: list[dict]) -> int:
    return len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))


class ContextBudget:
    def __init__(self, *, target_bytes: int = 12288, keep_recent_tool_outputs: int = 2,
                 compact_completed_exchanges: bool = False, retained_audit_entries: int = 8):
        if target_bytes <= 0 or keep_recent_tool_outputs < 1:
            raise ValueError("Expected a positive context target and recent-output count.")
        self.target_bytes = target_bytes
        self.keep_recent = keep_recent_tool_outputs
        self.compact_exchanges = compact_completed_exchanges
        self.audit_entries = retained_audit_entries
        if retained_audit_entries < 1:
            raise ValueError("Retain at least one completed-action reference.")
        self.events: list[dict] = []
        self.history_directory = None
        self.work = None

    def enable_assistant_archives(self, work: Path):
        self.work = work.resolve()
        self.history_directory = self.work / "temp" / "context-history"
        self.history_directory.mkdir(parents=True, exist_ok=True)

    def _compact_completed(self, messages, prepared):
        groups = []
        for index, message in enumerate(prepared):
            calls = message.get("tool_calls") or []
            if message.get("role") != "assistant" or not calls:
                continue
            if message.get("content") and (self.history_directory is None or not isinstance(message["content"], str)):
                continue
            indices = list(range(index + 1, index + 1 + len(calls)))
            if not all(i < len(messages) and prepared[i].get("role") == "tool"
                       and prepared[i].get("tool_call_id") == call["id"]
                       and messages[i].get("extra", {}).get("tool_output")
                       for i, call in zip(indices, calls)):
                continue
            groups.append((index, indices, calls))
        older = groups[:-self.keep_recent]
        if not older:
            return prepared, []
        removed, entries = set(), []
        for index, indices, calls in older:
            removed.add(index)
            removed.update(indices)
            assistant_path = None
            if prepared[index].get("content"):
                path = self.history_directory / f"{index:04d}.txt"
                encoded_content = prepared[index]["content"].encode("utf-8")
                if path.exists():
                    if path.read_bytes() != encoded_content:
                        raise RuntimeError("Saved assistant content changed.")
                else:
                    path.write_bytes(encoded_content)
                assistant_path = path.relative_to(self.work).as_posix()
            for i, call in zip(indices, calls):
                extra = messages[i]["extra"]
                record = extra["tool_output"]
                encoded = call["function"]["arguments"].encode("utf-8")
                entries.append({"sequence": record["sequence"], "returncode": extra.get("returncode"),
                    "exception_info": extra.get("exception_info", ""), "output": record["full_output_path"],
                    "arguments_preview": encoded[:128].decode("utf-8", errors="ignore"),
                    "arguments_truncated": len(encoded) > 128, "assistant_content_path": assistant_path})
        note = {"role": "assistant", "content": "Program-recorded completed actions, not a semantic summary. "
            "Complete commands/status/output references remain in temp/tool-output/events.jsonl. "
            + ("Complete earlier assistant text remains in temp/context-history/; filenames are original message indices. "
               if self.history_directory is not None else "")
            + json.dumps({"completed_actions": len(entries), "older_entries_in_audit_log": max(0, len(entries) - self.audit_entries),
                          "latest_completed": entries[-self.audit_entries:]}, ensure_ascii=False)}
        first = min(removed)
        projected = []
        for index, message in enumerate(prepared):
            if index == first:
                projected.append(note)
            if index not in removed:
                projected.append(message)
        return projected, [{"message_index": i, "full_output_path": messages[i]["extra"]["tool_output"]["full_output_path"]}
                           for _, indices, _ in older for i in indices]

    def project(self, messages: list[dict], prepared: list[dict], *, reserved_bytes: int = 0) -> list[dict]:
        if len(messages) != len(prepared):
            raise ValueError("Prepared messages no longer align with recorded messages.")
        projected = [dict(message) for message in prepared]
        before = message_bytes(projected)
        target = max(512, self.target_bytes - reserved_bytes)
        if self.compact_exchanges and before > target:
            projected, replaced = self._compact_completed(messages, projected)
            self.events.append({"before_bytes": before, "after_bytes": message_bytes(projected),
                "target_bytes": target, "reserved_bytes": reserved_bytes, "mode": "completed_exchanges", "replaced_outputs": replaced})
            return projected
        tool_indices = [i for i, message in enumerate(messages) if message.get("role") == "tool"]
        eligible = tool_indices[:-self.keep_recent]
        replacements = []
        if before > target:
            for index in eligible:
                if message_bytes(projected) <= target:
                    break
                extra = messages[index].get("extra", {})
                record = extra.get("tool_output")
                if not record:
                    continue
                path = record["full_output_path"]
                content = (f"<returncode>{extra.get('returncode')}</returncode>\n"
                           f"<output>[Earlier output archived at {path}; {record['raw_bytes']} UTF-8 bytes. "
                           f"Not a summary. Read needed lines with sed -n 'START,ENDp' {path}.]</output>")
                if len(content.encode("utf-8")) >= len(projected[index]["content"].encode("utf-8")):
                    continue
                projected[index]["content"] = content
                replacements.append({"message_index": index, "full_output_path": path,
                                     "returncode": extra.get("returncode"),
                                     "exception_info": extra.get("exception_info", "")})
                if extra.get("exception_info"):
                    projected[index]["content"] = f"<exception>{extra['exception_info']}</exception>\n" + content
        self.events.append({"before_bytes": before, "after_bytes": message_bytes(projected),
                            "target_bytes": target, "reserved_bytes": reserved_bytes, "replaced_outputs": replacements})
        return projected
