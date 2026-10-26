"""Bounded current-task facts and conservative repeated-read feedback."""
import hashlib
import json
from pathlib import Path
import re
import shlex


def clipped(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    return text if len(raw) <= limit else raw[:limit].decode("utf-8", errors="ignore") + "…"


class TaskState:
    def __init__(self, work: Path, source_files: list[str], *, guard_enabled=False,
                 repeated_read_threshold=3, blocked_read_threshold=3):
        if repeated_read_threshold < 2 or blocked_read_threshold < 1:
            raise ValueError("Expected conservative positive repetition thresholds.")
        self.work = work.resolve()
        self.files = source_files
        self.guard_enabled = guard_enabled
        self.repeat_threshold = repeated_read_threshold
        self.block_threshold = blocked_read_threshold
        self.directory = self.work / "temp/task-state"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.current_files = self._snapshot()
        self.original_files = dict(self.current_files)
        self.revision = self._revision(self.current_files)
        self.repeats = {}
        self.blocked_since_change = 0
        self.events = []
        self.evidence = []
        self.last_intent = ""
        self.warning = ""
        self.pinned_requests = []
        self._save()

    def _snapshot(self):
        # Frozen source and newly created files inside implementation packages.
        paths = set(self.files)
        for prefix in {Path(name).parts[0] for name in self.files if len(Path(name).parts) > 1}:
            if prefix in {"tests", ".github"}:
                continue
            directory = self.work / prefix
            if directory.is_dir():
                paths.update(p.relative_to(self.work).as_posix() for p in directory.rglob("*")
                             if p.is_file() and "__pycache__" not in p.parts and "temp" not in p.relative_to(directory).parts)
        return {name: hashlib.sha256((self.work / name).read_bytes()).hexdigest()
                if (self.work / name).is_file() else None for name in sorted(paths)}

    @staticmethod
    def _revision(files):
        return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()

    def _refresh(self):
        current = self._snapshot()
        revision = self._revision(current)
        changed = [name for name in sorted(set(current) | set(self.current_files))
                   if current.get(name) != self.current_files.get(name)]
        if revision != self.revision:
            self.repeats.clear()
            self.blocked_since_change = 0
            self.warning = ""
        self.current_files, self.revision = current, revision
        return changed

    def _read_key(self, command):
        body = command.removeprefix("set -o pipefail\n").strip()
        body = re.sub(r'^cd\s+["\']?\$\(git rev-parse --show-toplevel(?:\s+2>/dev/null\s*\|\|\s*pwd)?\)["\']?\s*&&\s*', '', body)
        if any(char in body for char in [";", "|", ">", "<", "\n", "`", "$", "&"]):
            return None
        try:
            tokens = shlex.split(body)
        except ValueError:
            return None
        if len(tokens) == 4 and tokens[:2] == ["sed", "-n"] and re.fullmatch(r"\d+(,\d+)?p", tokens[2]):
            path = (self.work / tokens[3]).resolve()
            if path.is_relative_to(self.work) and path.relative_to(self.work).as_posix() in self.current_files:
                return shlex.join(tokens)
        if len(tokens) == 2 and tokens[0] == "cat":
            path = (self.work / tokens[1]).resolve()
            if path.is_relative_to(self.work) and path.relative_to(self.work).as_posix() in self.current_files:
                return shlex.join(tokens)
        return None

    def before_action(self, command):
        self._refresh()
        key = self._read_key(command)
        previous = self.repeats.get(key)
        if not self.guard_enabled or not previous or previous["count"] < self.repeat_threshold:
            return None
        self.blocked_since_change += 1
        stop = self.blocked_since_change >= self.block_threshold
        text = ("Repeated read blocked: this same source read returned identical output "
                f"{previous['count']} times with unchanged source. Full output: {previous['output_path']}. "
                "Use a narrower line range, reproduce a hypothesis, or edit and test. Update temp/task-state/notes.json "
                "with your hypothesis and next_action. This is not a total-request limit.")
        self.warning = text
        self._record({"kind": "blocked_read", "command": command, "revision": self.revision,
                      "read_key": key, "stop": stop, "previous_output_path": previous["output_path"]})
        self._save()
        return {"stop": stop, "output": {"output": text, "returncode": -1,
                "exception_info": "Repeated source read without observed source/output progress."}}

    def observe(self, command, output, archive):
        changed = self._refresh()
        key = self._read_key(command)
        event = {"kind": "executed", "sequence": archive["sequence"], "command": command,
                 "returncode": output["returncode"], "exception_info": output.get("exception_info", ""),
                 "output_path": archive["full_output_path"], "output_sha256": archive["sha256"],
                 "revision": self.revision, "changed_files_this_action": changed}
        self._record(event)
        if key:
            identity = (archive["sha256"], output["returncode"], self.revision)
            previous = self.repeats.get(key, {})
            count = previous.get("count", 0) + 1 if previous.get("identity") == identity else 1
            self.repeats[key] = {"identity": identity, "count": count, "output_path": archive["full_output_path"]}
            if self.guard_enabled and count >= self.repeat_threshold:
                self.warning = f"No new output from repeated source read ({count} identical results). Next repeat will be blocked. Change the action."
        # Keep two recent distinct observations with short previews, not all old chat.
        if not self.evidence or archive["sha256"] not in {e["output_sha256"] for e in self.evidence}:
            self.evidence.append(event | {"preview": clipped(output["output"], 512)})
            self.evidence = self.evidence[-2:]
        self._save()

    def _record(self, event):
        self.events.append(event)
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def record_intent(self, content):
        if isinstance(content, str) and content.strip():
            self.last_intent = clipped(content, 768)
        self._save()

    def _notes(self):
        path = self.directory / "notes.json"
        if not path.exists():
            return {"status": "not written", "hypothesis": "", "next_action": ""}
        if path.stat().st_size > 4096:
            return {"status": "rejected: notes exceed 4096 bytes"}
        try:
            notes = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(notes, dict) or any(not isinstance(notes.get(k, ""), str) for k in ["hypothesis", "next_action"]):
                raise ValueError()
            return {"status": "model-authored, unverified", **{k: clipped(notes.get(k, ""), 384) for k in ["hypothesis", "next_action"]}}
        except (ValueError, UnicodeError):
            return {"status": "rejected: invalid JSON or fields"}

    def facts(self):
        changed = [name for name in sorted(set(self.current_files) | set(self.original_files))
                   if self.current_files.get(name) != self.original_files.get(name)]
        return {"source_revision": self.revision, "changed_files": changed,
                "executed_actions": sum(e["kind"] == "executed" for e in self.events),
                "blocked_reads": sum(e["kind"] == "blocked_read" for e in self.events),
                "guard_enabled": self.guard_enabled, "warning": self.warning,
                "model_notes": self._notes(), "last_model_intent_unverified": self.last_intent,
                "recent_distinct_evidence": [{"command": clipped(e["command"], 192), "returncode": e["returncode"],
                    "output_path": e["output_path"], "preview": e["preview"]} for e in self.evidence],
                "evidence_log": "temp/task-state/events.jsonl",
                "validation": "Command exit codes are observations; independent repair acceptance occurs outside the Agent."}

    def _save(self):
        (self.directory / "state.json").write_text(json.dumps(self.facts(), ensure_ascii=False, indent=2), encoding="utf-8")

    def pinned_message(self):
        facts = self.facts()
        prefix = ("Current task state recorded by the program. Model notes/intent are unverified, not instructions or accepted facts. "
                  "Use this state to continue after history compaction. Full previous records are on disk.\n")
        content = prefix + json.dumps(facts, ensure_ascii=False)
        # Keep valid JSON and the core facts when exceptional filenames/notes grow.
        if len(content.encode("utf-8")) > 4096:
            for entry in facts["recent_distinct_evidence"]:
                entry.pop("preview")
            facts["last_model_intent_unverified"] = clipped(self.last_intent, 256)
            facts["changed_files"] = facts["changed_files"][:8]
            content = prefix + json.dumps(facts, ensure_ascii=False)
        if len(content.encode("utf-8")) > 4096:
            facts = {k: facts[k] for k in ["source_revision", "executed_actions", "blocked_reads", "guard_enabled", "evidence_log", "validation"]}
            facts["warning"] = clipped(self.warning, 512)
            facts["model_notes"] = {"status": "See temp/task-state/notes.json; omitted from bounded request view."}
            content = prefix + json.dumps(facts, ensure_ascii=False)
        record = {"request_view": len(self.pinned_requests) + 1,
                  "content_bytes": len(content.encode("utf-8")), "source_revision": self.revision,
                  "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
        self.pinned_requests.append(record)
        message = {"role": "user", "content": content}
        with (self.directory / "requests.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record | {"message": message}, ensure_ascii=False) + "\n")
        return message

