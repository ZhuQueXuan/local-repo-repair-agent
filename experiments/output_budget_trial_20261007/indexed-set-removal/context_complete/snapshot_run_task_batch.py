"""One original-Agent run per task, with frozen evaluation and shared accounting."""
import argparse
from datetime import datetime
import difflib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.dont_write_bytecode = True
from run_deepseek_smoke import BudgetedDeepSeekModel, GitBashEnvironment
from dotenv import dotenv_values
from minisweagent.agents.default import DefaultAgent
from task_workspace import ROOT, changed_files, digest, load_task, prepare_task
from tool_output_budget import ToolOutputBudget
from context_budget import ContextBudget


class TaskEnvironment(GitBashEnvironment):
    def execute(self, action, cwd="", *, timeout=None):
        return super().execute(action | {"command": "set -o pipefail\n" + action["command"]}, cwd, timeout=timeout)


class BoundedTaskEnvironment(TaskEnvironment):
    def __init__(self, work, bash, *, max_output_bytes):
        super().__init__(work, bash)
        self.output_budget = ToolOutputBudget(Path(work), max_output_bytes=max_output_bytes)

    def _process_output(self, output, *, command):
        return self.output_budget.process(output, command=command)

    def serialize(self):
        data = super().serialize()
        data["info"]["tool_output_budget"] = self.output_budget.statistics()
        return data


class RecordedModel(BudgetedDeepSeekModel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.request_measurements = []

    def _query(self, messages, **kwargs):
        measurement = {"next_request": self.requests + 1,
                       "message_utf8_bytes": len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))}
        self.request_measurements.append(measurement)
        before = self.requests
        try:
            return super()._query(messages, **kwargs)
        finally:
            measurement["http_attempted"] = self.requests > before

    def _calculate_cost(self, response):
        result = super()._calculate_cost(response)
        self.usage[-1]["response_model"] = response.model
        return result


class ContextManagedModel(RecordedModel):
    def __init__(self, *args, context_policy, **kwargs):
        super().__init__(*args, **kwargs)
        if context_policy["target_bytes"] >= self.budget_config["max_messages_utf8_bytes"]:
            raise ValueError("Context target must leave room below the hard request guard.")
        self.context_budget = ContextBudget(**context_policy)

    def _prepare_messages_for_api(self, messages):
        return self.context_budget.project(messages, super()._prepare_messages_for_api(messages))


def evaluate(name: str, work: Path, output: Path) -> dict:
    result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/check_task.py"), "--task", name,
                            "--repo", str(work), "--output", str(output), "--full-suite"],
                            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=240)
    if not output.is_file():
        raise RuntimeError("Independent evaluation did not produce a report: exit " + str(result.returncode))
    report = json.loads(output.read_text(encoding="utf-8"))
    expected = 0 if report["task_tests_passed"] and report["full_upstream_suite"]["successful"] else 1
    if result.returncode != expected:
        raise RuntimeError("Evaluator exit/report mismatch.")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--config", default="local_baseline_20261007.json",
                        choices=("local_baseline_20261007.json", "output_budget_trial_20261007.json"))
    parser.add_argument("--variant", choices=("raw", "bounded", "context_managed", "context_compacted", "context_complete"), default="raw")
    args = parser.parse_args()
    config = json.loads((ROOT / "configs" / args.config).read_text(encoding="utf-8"))
    variants = config.get("variants", {"raw": {"tool_output_budget": None}})
    if args.variant not in variants:
        raise SystemExit("Variant is not configured for this batch.")
    output_policy = variants[args.variant]["tool_output_budget"]
    context_policy = variants[args.variant].get("context_budget")
    archive_assistant = variants[args.variant].get("archive_assistant_content", False)
    if args.task not in config["tasks"]:
        raise SystemExit("Task is not in this fixed batch.")
    identity = config["batch_id"] + "_" + args.task
    if "variants" in config:
        identity += "_" + args.variant
    claim = ROOT / "temp" / (identity + "_claim.json")
    if args.run and claim.exists():
        raise SystemExit("This paid task run already started; preserve its evidence.")
    work, manifest = prepare_task(args.task)
    results = ROOT / "temp" / (identity + "_" + datetime.now().strftime("%H%M%S_%f"))
    results.mkdir()
    baseline = evaluate(args.task, work, results / "baseline.json")
    if baseline["issue_checks"]["successful"] or not baseline["upstream_targeted"]["successful"]:
        raise RuntimeError("Expected reproduced issue with passing upstream targeted tests.")
    settings = dotenv_values(ROOT / ".env", interpolate=False)
    model_options = {"ledger_path": results / "ledger.json", "error_path": results / "request_error.json"}
    model = (ContextManagedModel(settings, context_policy=context_policy, **model_options) if context_policy is not None
             else RecordedModel(settings, **model_options))
    if archive_assistant:
        model.context_budget.enable_assistant_archives(work)
    environment = TaskEnvironment(work, Path(r"D:\Git\bin\bash.exe"))
    probe = environment.execute({"command": "git rev-parse --show-toplevel; python -B -c 'import sys; print(sys.prefix)'"})
    if probe["returncode"] != 0 or str(ROOT / ".venv").lower() not in probe["output"].lower():
        raise RuntimeError("Git Bash / virtual environment preflight failed.")
    # This assertion uses the real shell and must fail the pipeline overall.
    pipeline = environment.execute({"command": "python -B -c 'raise SystemExit(7)' | tail -1"})
    if pipeline["returncode"] == 0:
        raise RuntimeError("Pipeline hid an earlier command failure.")
    if settings["DEEPSEEK_API_KEY"] in json.dumps(model.serialize()):
        raise RuntimeError("Credentials appeared in serialized configuration.")
    preflight = {"task_id": manifest["task_id"], "work": str(work), "git_root_verified": True,
                 "pipefail_verified": True, "variant": args.variant, "tool_output_budget": output_policy,
                 "context_budget": context_policy,
                 "archive_assistant_content": archive_assistant,
                 "external_model_requests": 0, "budget": model.budget.snapshot()}
    (results / "preflight.json").write_text(json.dumps(preflight, indent=2), encoding="utf-8")
    if not args.run:
        print(json.dumps(preflight, indent=2))
        print("Temporary results: " + str(results))
        return
    with claim.open("x", encoding="utf-8") as stream:
        json.dump({"results": str(results), "work": str(work), "task": args.task}, stream, indent=2)
    if output_policy is not None:
        environment = BoundedTaskEnvironment(work, Path(r"D:\Git\bin\bash.exe"), **output_policy)
    current_config = config | {"variant": args.variant, "tool_output_budget": output_policy, "context_budget": context_policy,
                               "archive_assistant_content": archive_assistant,
                               "model_name": settings["DEEPSEEK_CHAT_MODEL"], "task_manifest": manifest,
                               "budget": model.budget_config, "step_limit": 0}
    (results / "run_config.json").write_text(json.dumps(current_config, indent=2), encoding="utf-8")
    for name in ("run_task_batch.py", "task_workspace.py", "check_task.py", "run_deepseek_smoke.py", "deepseek_budget.py", "tool_output_budget.py", "context_budget.py"):
        shutil.copy2(ROOT / "scripts" / name, results / ("snapshot_" + name))
    for name in ("issue.md", "checks/test_behavior.py", "manifest.json"):
        shutil.copy2(ROOT / "tasks" / args.task / name, results / Path(name).name)
    agent = DefaultAgent(model, environment,
        system_template="You are a helpful software engineering assistant. Use exactly one bash tool call per response.",
        instance_template=("Fix this issue: {{task}}\n"
            "Work only in this checkout. It has an independent Git baseline commit. Do not access parent directories, "
            "unrelated projects, the web, or install dependencies. Do not delete files. Put temporary helpers in temp/. "
            "Do not edit tests/, task_checks/, TASK.md, or project metadata. Change only implementation files. "
            "Search for relevant symbols and read short line ranges; avoid whole large files or long test output. "
            "Inspect, reproduce, repair, then run both:\n" + manifest["agent_test_command"] + "\n"
            "python -B -m unittest discover -s task_checks -v\n"
            "Commands run in fresh Git Bash shells on Windows, using the available virtualenv Python. "
            "Shell pipelines propagate failures. When finished, run echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT by itself."),
        step_limit=0, cost_limit=float(model.budget.limit), wall_time_limit_seconds=config["wall_time_limit_seconds"],
        output_path=results / "trajectory.json")
    start = time.monotonic()
    try:
        status = agent.run((ROOT / "tasks" / args.task / "issue.md").read_text(encoding="utf-8"))
    except Exception as error:
        status = {"exit_status": type(error).__name__}
    finally:
        model.ledger.update(completed=True, note="This run ended. Shared total budget remains cumulative.")
        (results / "ledger.json").write_text(json.dumps(model.ledger, indent=2), encoding="utf-8")
    elapsed = time.monotonic() - start
    load_task(args.task)  # Frozen external inputs must still be intact.
    verification = evaluate(args.task, work, results / "verification.json")
    changed = changed_files(work, manifest)
    task, _ = load_task(args.task)
    protected = all((work / ("TASK.md" if relative == "issue.md" else "task_checks/test_behavior.py")).is_file()
                    and digest(work / ("TASK.md" if relative == "issue.md" else "task_checks/test_behavior.py")) == expected
                    for relative, expected in manifest["task_files_sha256"].items())
    unexpected = [relative for relative in changed if not relative.startswith(manifest["source_prefix"])]
    patch = []
    for relative in changed:
        if relative not in manifest["source_files_sha256"]:
            continue
        before = (task / "repo" / relative).read_text(encoding="utf-8").splitlines(keepends=True)
        after = (work / relative).read_text(encoding="utf-8").splitlines(keepends=True) if (work / relative).is_file() else []
        patch.extend(difflib.unified_diff(before, after, fromfile="a/" + relative, tofile="b/" + relative))
    patch_path = results / "repair.diff"
    patch_path.write_text("".join(patch), encoding="utf-8")
    before_full, after_full = baseline["full_upstream_suite"], verification["full_upstream_suite"]
    new_failures = sorted(set(after_full["failures"] + after_full["errors"]) - set(before_full["failures"] + before_full["errors"]))
    replay_passed = False
    if patch and not unexpected and protected and verification["task_tests_passed"] and not new_failures:
        replay, _ = prepare_task(args.task)
        subprocess.run(["git", "-C", str(replay), "apply", "--", str(patch_path)], check=True, capture_output=True)
        replay_result = evaluate(args.task, replay, results / "patch_verification.json")
        replay_full = replay_result["full_upstream_suite"]
        replay_new = set(replay_full["failures"] + replay_full["errors"]) - set(before_full["failures"] + before_full["errors"])
        replay_passed = replay_result["task_tests_passed"] and not replay_new
    summary = {"task_id": manifest["task_id"], "variant": args.variant, "exit_status": status.get("exit_status"),
               "limit_kind": status.get("limit_kind"), "work": str(work),
               "requests": model.requests, "estimated_cost_cny": model.actual_cost_cny, "usage": model.usage,
               "request_measurements": model.request_measurements,
               "tool_output_statistics": environment.output_budget.statistics() if output_policy is not None else None,
               "context_projections": model.context_budget.events if context_policy is not None else None,
               "elapsed_seconds": round(elapsed, 3), "changed_files": changed, "unexpected_changes": unexpected,
               "checks_and_description_unchanged": protected, "task_tests_passed": verification["task_tests_passed"],
               "new_full_suite_failures": new_failures, "patch_replay_passed": replay_passed,
               "repair_verified": verification["task_tests_passed"] and protected and not unexpected and not new_failures and replay_passed,
               "shared_budget": model.budget.snapshot(), "scope": manifest["scope"]}
    (results / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if output_policy is not None:
        shutil.copytree(environment.output_budget.directory, results / "tool-output")
    if archive_assistant:
        shutil.copytree(model.context_budget.history_directory, results / "context-history")
    if any(settings["DEEPSEEK_API_KEY"] in p.read_text(encoding="utf-8") for p in results.rglob("*") if p.is_file()):
        raise RuntimeError("Credentials found in results; do not publish.")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("Temporary results: " + str(results))


if __name__ == "__main__":
    main()
