"""Reversible request projection: archive references replace older tool bodies."""
import json


def message_bytes(messages: list[dict]) -> int:
    return len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))


class ContextBudget:
    def __init__(self, *, target_bytes: int = 12288, keep_recent_tool_outputs: int = 2):
        if target_bytes <= 0 or keep_recent_tool_outputs < 1:
            raise ValueError("Expected a positive context target and recent-output count.")
        self.target_bytes = target_bytes
        self.keep_recent = keep_recent_tool_outputs
        self.events: list[dict] = []

    def project(self, messages: list[dict], prepared: list[dict]) -> list[dict]:
        if len(messages) != len(prepared):
            raise ValueError("Prepared messages no longer align with recorded messages.")
        projected = [dict(message) for message in prepared]
        before = message_bytes(projected)
        tool_indices = [i for i, message in enumerate(messages) if message.get("role") == "tool"]
        eligible = tool_indices[:-self.keep_recent]
        replacements = []
        if before > self.target_bytes:
            for index in eligible:
                if message_bytes(projected) <= self.target_bytes:
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
                            "target_bytes": self.target_bytes, "replaced_outputs": replacements})
        return projected
