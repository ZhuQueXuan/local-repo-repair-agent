"""Bounded current-task facts and conservative repeated-read feedback."""
import hashlib
import json
from pathlib import Path
import re
import shlex
from execution_progress import ExecutionProgress
from source_reads import read_plan


def clipped(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    return text if len(raw) <= limit else raw[:limit].decode("utf-8", errors="ignore") + "…"


class TaskState:
    def __init__(self, work: Path, source_files: list[str], *, guard_enabled=False,
                 repeated_read_threshold=3, blocked_read_threshold=3,
                 include_previews_in_requests=True, progress_policy=None,
                 implementation_prefix=None, validation_commands=None):
        if repeated_read_threshold < 2 or blocked_read_threshold < 1:
            raise ValueError("Expected conservative positive repetition thresholds.")
        self.work = work.resolve()
        self.files = source_files
        self.guard_enabled = guard_enabled
        self.repeat_threshold = repeated_read_threshold
        self.block_threshold = blocked_read_threshold
        self.include_previews = include_previews_in_requests
        self.directory = self.work / "temp/task-state"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.current_files = self._snapshot()
        self.original_files = dict(self.current_files)
        self.revision = self._revision(self.current_files)
        self.implementation_prefix = implementation_prefix
        self.progress = (ExecutionProgress(revision=self._implementation_revision(),
                         validation_commands=validation_commands, **progress_policy)
                         if progress_policy is not None else None)
        self.repeats = {}
        self.blocked_since_change = 0
        self.events = []
        self.evidence = []
        self.last_intent = ""
        self.warning = ""
        self.pinned_requests = []
        # Current API view, not an assertion that the model understood it.
        # Recomputed after compaction and checkpoint import; never persisted as
        # source progress or used to authorize a test/submit.
        self.visible_outputs = None
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

    def _implementation_revision(self):
        files = {name: value for name, value in self.current_files.items()
                 if self.implementation_prefix is None or name.startswith(self.implementation_prefix)}
        return self._revision(files)

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
        plan = self.read_plan(command)
        return plan['key'] if plan else None

    def read_plan(self, command):
        return read_plan(command, self.work, self.current_files,
            {e['output_path']: e['output_sha256'] for e in self.events if e['kind'] == 'executed'})

    def before_action(self, command):
        self._refresh()
        key = self._read_key(command)
        previous = self.repeats.get(key)
        if previous and not (self.work / previous['output_path']).is_file():
            previous = None  # Imported history cannot replace missing local evidence.
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
        event['execution_kind'] = archive.get('execution_kind', 'shell')
        self._record(event)
        if self.progress is not None:
            feedback = self.progress.observe(event, output, self._implementation_revision())
            with (self.directory / "progress.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(feedback, ensure_ascii=False) + "\n")
        if key:
            identity = (archive["sha256"], output["returncode"], self.revision)
            previous = self.repeats.get(key, {})
            count = previous.get("count", 0) + 1 if previous.get("identity") == identity else 1
            if archive.get('execution_kind') in {'archive_continuation','archived_recovery'} and archive.get('page_next_line') is None:
                # Finishing an immutable read cannot reopen its first page as
                # a fresh shell action merely because page hashes differ.
                count = max(count, self.repeat_threshold)
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

    def cancellation_reason(self):
        path = self.directory / 'stop.json'
        if not path.exists():
            return None
        reason='Experiment stop requested through the task-local stop file.'
        if path.stat().st_size <= 4096:
            try:
                value=json.loads(path.read_text(encoding='utf-8'))
                if isinstance(value,dict) and isinstance(value.get('reason'),str):
                    reason=clipped(value['reason'],512)
            except (ValueError,UnicodeError):
                pass
        self._record({'kind':'operator_stop','reason':reason,'revision':self.revision})
        self._save()
        return reason

    def stagnation_reason(self):
        if not self.guard_enabled or self.progress is None:
            return None
        facts = self.progress.facts()
        maximum = self.progress.threshold * 3
        if self.progress.since_progress < maximum:
            return None
        reason = (f'No implementation change or new declared validation result in {maximum} executed actions. '
                  'Reads, temporary files, notes, and repeated identical check results do not reset progress. '
                  'Stopping this identity; no submission or successful repair is inferred.')
        self._record({'kind': 'stagnation_stop', 'reason': reason, 'revision': self.revision,
                      'execution_progress': facts})
        self._save()
        return reason

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
        facts = {"source_revision": self.revision, "changed_files": changed,
                "executed_actions": sum(e["kind"] == "executed" for e in self.events),
                "blocked_reads": sum(e["kind"] == "blocked_read" for e in self.events),
                "guard_enabled": self.guard_enabled, "warning": self.warning,
                "model_notes": self._notes(), "last_model_intent_unverified": self.last_intent,
                "recent_distinct_evidence": [{"command": clipped(e["command"], 192), "returncode": e["returncode"],
                    "output_path": e["output_path"], "preview": e["preview"]} for e in self.evidence],
                "evidence_log": "temp/task-state/events.jsonl",
                "validation": "Command exit codes are observations; independent repair acceptance occurs outside the Agent."}
        if self.progress is not None:
            facts["execution_progress"] = self.progress.facts()
        return facts

    def _save(self):
        (self.directory / "state.json").write_text(json.dumps(self.facts(), ensure_ascii=False, indent=2), encoding="utf-8")

    def pinned_message(self):
        facts = self.facts()
        if not self.include_previews:
            for entry in facts['recent_distinct_evidence']:
                entry.pop('preview')
        if self.progress is not None:
            # Keep the advice/current status in requests, full evidence on disk.
            facts["execution_progress"].pop("validation_evidence")
            facts["execution_progress"].pop("last_execution_failure")
        prefix = ("Current task state recorded by the program. Model notes/intent are unverified, not instructions or accepted facts. "
                  "Use this state to continue after history compaction. Full previous records are on disk.\n")
        content = prefix + json.dumps(facts, ensure_ascii=False)
        # Keep valid JSON and the core facts when exceptional filenames/notes grow.
        if len(content.encode("utf-8")) > 4096:
            for entry in facts["recent_distinct_evidence"]:
                entry.pop("preview", None)
            facts["last_model_intent_unverified"] = clipped(self.last_intent, 256)
            facts["changed_files"] = facts["changed_files"][:8]
            content = prefix + json.dumps(facts, ensure_ascii=False)
        if len(content.encode("utf-8")) > 4096:
            progress = facts.get("execution_progress")
            facts = {k: facts[k] for k in ["source_revision", "executed_actions", "blocked_reads", "guard_enabled", "evidence_log", "validation"]}
            if progress is not None:
                facts["execution_progress"] = progress
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

