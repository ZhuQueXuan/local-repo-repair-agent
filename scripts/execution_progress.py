"""Advisory next actions derived from source revisions and observed test runs.

No model calls, repair answers, submission acceptance, or additional stop limit.
Only declared test selections with recognizable runner summaries earn a status.
"""
import hashlib
import json
import re
import shlex


def runner_tokens(command):
    """Extract one simple Python test invocation; decline ambiguous shell bodies."""
    body = command.removeprefix("set -o pipefail\n").strip()
    if "<<" in body or "`" in body:
        return None
    try:
        lexer = shlex.shlex(body, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None
    # Recognize the existing Click checkout command; this only classifies an
    # observation and never evaluates assignments or executes shell text.
    if tokens[:1] in (["PYTHONPATH=src"], ["PYTHONPATH=."]):
        tokens = tokens[1:]
    candidates = []
    for i, token in enumerate(tokens):
        if token not in {"python", "python3"} or (i and tokens[i - 1] not in {"&&", ";"}):
            continue
        end = next((j for j in range(i + 1, len(tokens)) if tokens[j] in {"&&", ";", "||", "|", ">", ">&"}), len(tokens))
        invocation = tokens[i:end]
        # 2>&1 occurs just before the tail pipeline.
        if invocation[-1:] == ["2"]:
            invocation = invocation[:-1]
        invocation = [t for t in invocation if t not in {"-B", "-q", "-v", "-vv"}]
        if invocation[:3] not in (["python", "-m", "pytest"], ["python", "-m", "unittest"],
                                 ["python3", "-m", "pytest"], ["python3", "-m", "unittest"]):
            continue
        suffix = tokens[end:]
        if suffix[:3] == [">&", "1", "|"]:
            suffix = suffix[2:]
        if suffix and (suffix[:2] != ["|", "tail"] or any(t in {"&&", ";", "||", "|", ">", ">&"} for t in suffix[1:])):
            return None
        candidates.append(tuple(["python"] + invocation[1:]))
    return candidates[0] if len(candidates) == 1 else None


def observed_test_result(tokens, output):
    """Exit zero alone (or a claim of success) is insufficient test evidence."""
    text = output["output"]
    if output.get("exception_info"):
        return "unknown", []
    if tokens[2] == "unittest":
        count = re.search(r"(?m)^Ran (\d+) tests? in .+$", text)
        passed = count and int(count[1]) > 0 and re.search(r"(?m)^OK(?: \(.*\))?\s*$", text)
        skipped = re.search(r"(?m)^OK \(skipped=(\d+)\)\s*$", text)
        if count and skipped and int(skipped[1]) >= int(count[1]):
            passed = False
        failed = count and re.search(r"(?m)^FAILED \(.*\)\s*$", text)
    else:
        summary = next((line.strip("= \r") for line in reversed(text.splitlines())
                        if re.search(r"\d+ (?:passed|failed|errors?|skipped|deselected)", line)), "")
        passed = re.search(r"\b[1-9]\d* passed\b", summary) and not re.search(r"\b\d+ (?:failed|errors?)\b", summary)
        failed = re.search(r"\b[1-9]\d* (?:failed|errors?)\b", summary)
    result = "passed" if passed and output["returncode"] == 0 else "failed" if failed and output["returncode"] != 0 else "unknown"
    failures = [line[:160] for line in text.splitlines()
                if re.match(r"^(?:FAIL:|ERROR:|FAILED |ERROR |\w*(?:Error|Exception):)", line)][:4]
    return result, failures


class ExecutionProgress:
    def __init__(self, *, revision, validation_commands, stagnation_actions=8):
        if stagnation_actions < 2:
            raise ValueError("Stagnation feedback must allow at least two actions.")
        self.revision = revision
        self.initial_revision = revision
        self.commands = {key: runner_tokens(value) for key, value in validation_commands.items()}
        if not self.commands or any(value is None for value in self.commands.values()):
            raise ValueError("Expected declared simple test commands.")
        self.threshold = stagnation_actions
        self.actions = 0
        self.since_progress = 0
        self.first_change = None
        self.validation = {}
        self.seen_results = set()
        self.last_execution_failure = None
        self.plan_version = 0
        self.last_plan = None

    def export_state(self):
        return {"schema_version": 1, "commands": self.commands, "threshold": self.threshold,
                **{key: getattr(self, key) for key in ("initial_revision", "revision", "actions", "since_progress",
                    "first_change", "validation", "last_execution_failure", "plan_version", "last_plan")},
                "seen_results": sorted(self.seen_results)}

    def restore_state(self, value):
        expected = self.export_state()
        if set(value) != set(expected) or value["schema_version"] != 1 or value["threshold"] != self.threshold or {
                k: tuple(v) for k, v in value["commands"].items()} != self.commands:
            raise ValueError("Checkpoint progress policy mismatch")
        if value["revision"] != self.revision:
            raise ValueError("Checkpoint progress source revision mismatch")
        for key in ("initial_revision", "revision", "actions", "since_progress", "first_change", "validation",
                    "last_execution_failure", "plan_version", "last_plan"):
            setattr(self, key, value[key])
        self.seen_results = {(scope, revision, result, tuple(failures))
                             for scope, revision, result, failures in value["seen_results"]}

    def observe(self, event, output, revision):
        self.actions += 1
        changed = revision != self.revision
        if changed and self.first_change is None:
            self.first_change = event["sequence"]
        self.revision = revision
        tokens = runner_tokens(event["command"])
        scope = next((key for key, value in self.commands.items() if value == tokens), None)
        new_result = False
        if scope:
            result, failures = observed_test_result(tokens, output)
            record = {"result": result, "revision": revision, "sequence": event["sequence"],
                      "output_path": event["output_path"], "failures": failures}
            identity = (scope, revision, result, tuple(failures))
            new_result = result != "unknown" and identity not in self.seen_results
            self.seen_results.add(identity)
            self.validation[scope] = record
        if output["returncode"] != 0 or output.get("exception_info"):
            self.last_execution_failure = {"sequence": event["sequence"], "output_path": event["output_path"],
                                           "returncode": output["returncode"]}
        self.since_progress = 0 if changed or new_result else self.since_progress + 1
        # Notes, read ranges, temp scripts, output timestamps and claims do not reset progress.
        plan = self.facts()["next_action"]
        if plan != self.last_plan:
            self.plan_version += 1
            self.last_plan = plan
        return {"sequence": event["sequence"], "implementation_changed": changed,
                "new_validation_result": new_result, **self.facts()}

    def facts(self):
        current = {scope: (record["result"] if record["revision"] == self.revision else "stale")
                   for scope, record in self.validation.items()}
        failed = [scope for scope, result in current.items() if result == "failed"]
        missing = [scope for scope in self.commands if current.get(scope) != "passed"]
        dirty = self.revision != self.initial_revision
        if failed:
            phase, next_action = "revise_after_failure", "Inspect the failing validation output; revise the hypothesis and make a minimal repair, then rerun that validation."
        elif dirty and missing:
            phase, next_action = "validate_changes", "Run the declared checks for the current source revision: " + ", ".join(missing) + ". Earlier passes are stale after edits."
        elif dirty:
            phase, next_action = "review_and_submit", "Review the diff and constraints, then submit. Observed passes are not independent repair acceptance."
        else:
            phase, next_action = "diagnose_and_repair", "Run a failing issue check or a concrete reproduction; if the diagnosis is sufficient, make the smallest implementation change and validate."
        stalled = self.since_progress >= self.threshold
        if stalled:
            next_action += " Progress has stalled: stop broad rereading. Take the next concrete action or name the specific missing evidence and obtain only that evidence."
        return {"phase": phase, "plan_version": self.plan_version,
                "actions_without_source_or_new_validation": self.since_progress,
                "stagnation_feedback": stalled, "first_source_change_sequence": self.first_change,
                "current_validation": {scope: current.get(scope, "not_run") for scope in self.commands},
                "validation_evidence": self.validation, "last_execution_failure": self.last_execution_failure,
                "next_action": next_action, "status": "program-derived advisory; no independent acceptance"}

    @staticmethod
    def revision(files):
        return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
