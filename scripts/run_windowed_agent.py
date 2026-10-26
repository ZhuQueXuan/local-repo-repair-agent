"""Run one bounded real-repository task, with independent frozen evaluation."""
import argparse
from datetime import datetime
import difflib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
TASK = ROOT / "tasks" / "windowed-empty"
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "scripts"))

from run_deepseek_smoke import BudgetedDeepSeekModel, GitBashEnvironment
from dotenv import dotenv_values
from minisweagent.agents.default import DefaultAgent
from minisweagent.exceptions import LimitsExceeded
from deepseek_budget import load_budget_config


class RecordedModel(BudgetedDeepSeekModel):
    def _calculate_cost(self, response):
        output = super()._calculate_cost(response)
        self.usage[-1]["response_model"] = response.model
        return output


def evaluate(work, output):
    result = subprocess.run(
        [sys.executable, "-B", str(ROOT / "scripts" / "check_windowed_task.py"),
         "--repo", str(work), "--full-suite", "--output", str(output)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    if not output.is_file():
        raise RuntimeError("Independent evaluator did not produce a report.")
    report = json.loads(output.read_text(encoding="utf-8"))
    assert result.returncode == (0 if report["task_tests_passed"] and report["full_upstream_suite"]["successful"] else 1)
    return report


def changed_inputs(work, manifest):
    changed = [name for name, digest in manifest["source_files_sha256"].items()
               if not (work / name).is_file() or hashlib.sha256((work / name).read_bytes()).hexdigest() != digest]
    extra = [path.relative_to(work).as_posix() for path in work.rglob("*") if path.is_file()
             and path.relative_to(work).parts[0] not in (".git", "temp", "task_checks")
             and path.relative_to(work).as_posix() not in manifest["source_files_sha256"]
             and path.relative_to(work).as_posix() != "TASK.md"]
    return sorted(changed + extra)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="Run this bounded batch using the configured API.")
    args = parser.parse_args()
    config = json.loads((ROOT / "configs" / "windowed_first_batch.json").read_text(encoding="utf-8"))
    # Keep the completed first-batch identity; current policy overrides its old limits.
    policy = load_budget_config()
    config.update({key: policy[key] for key in ("max_requests", "reservation_per_attempt_cny",
                  "max_messages_utf8_bytes", "max_output_tokens",
                  "conservative_input_cny_per_million", "conservative_output_cny_per_million")})
    config.pop("max_reserved_cost_cny", None)
    config["max_total_cost_cny"] = policy["max_total_cost_cny"]
    assert config["max_messages_utf8_bytes"] == 16384 and config["max_output_tokens"] == 2048
    assert config["command_timeout_seconds"] == 30 and config["model_temperature"] == 0
    assert config["thinking_mode"] == "disabled" and config["api_retries"] == 0
    assert config["model_query_retry_attempts"] == 1
    ledger_path = ROOT / "temp" / ("deepseek_" + config["batch_id"] + "_ledger.json")
    claim_path = ROOT / "temp" / (config["batch_id"] + "_claim.json")
    if args.run and claim_path.exists():
        raise SystemExit("This task batch was already started; do not start another paid run.")
    settings = dotenv_values(ROOT / ".env", interpolate=False)
    if not all(settings.get(name) for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_API_BASE_URL", "DEEPSEEK_CHAT_MODEL")):
        raise SystemExit("Required model configuration is missing.")
    if urlparse(settings["DEEPSEEK_API_BASE_URL"]).hostname != "api.deepseek.com" or settings["DEEPSEEK_CHAT_MODEL"] != "deepseek-flash":
        raise SystemExit("This budget is configured for official DeepSeek Flash only.")
    from execution_runtime import bash_executable
    bash = bash_executable()
    if not bash.is_file():
        raise SystemExit("Configured Git Bash was not found.")
    setup = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "prepare_windowed_task.py")],
                           cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True)
    work = Path(json.loads(setup.stdout)["work"])
    result_dir = ROOT / "temp" / (config["batch_id"] + "_" + datetime.now().strftime("%H%M%S_%f"))
    result_dir.mkdir()
    (ROOT / "temp" / "bash-home").mkdir(exist_ok=True)
    manifest = json.loads((TASK / "manifest.json").read_text(encoding="utf-8"))
    assert not changed_inputs(work, manifest)
    original_check = (TASK / "checks" / "test_issue_462.py").read_bytes()
    original_issue = (TASK / "issue.md").read_bytes()
    old_ledger = ROOT / "temp" / "deepseek_first_task_ledger.json"
    old_ledger_bytes = old_ledger.read_bytes()
    baseline = evaluate(work, result_dir / "baseline.json")
    assert baseline["upstream_windowed"]["passed"] == 6
    assert baseline["issue_checks"]["passed"] == 5 and len(baseline["issue_checks"]["failures"]) == 5
    assert baseline["protected_tests_unchanged"]
    env = GitBashEnvironment(work, bash)
    probe = env.execute({"command": "python -B -c 'import sys; print(sys.prefix)'"})
    assert probe["returncode"] == 0 and str(ROOT / ".venv").lower() in probe["output"].lower()
    model = RecordedModel(settings, ledger_path=ledger_path, error_path=result_dir / "request_error.json")
    assert not model.contains_credentials(json.dumps(model.serialize()))
    for messages, count in (([{"role": "user", "content": "x" * 17000}], 0),):
        model.requests = count
        try:
            model._query(messages)
        except LimitsExceeded:
            pass
        else:
            raise AssertionError("A pre-request guard failed.")
        assert model.requests == count
    model.requests = 0
    preflight = {"batch_id": config["batch_id"], "work": str(work), "result_dir": str(result_dir),
                 "baseline_issue_failures": len(baseline["issue_checks"]["failures"]),
                 "virtualenv_python": "verified", "credentials_in_model_config": False,
                 "context_guard": "passed", "old_ledger_unchanged": old_ledger.read_bytes() == old_ledger_bytes,
                 "requests": 0, "shared_budget": model.budget.snapshot()}
    (result_dir / "preflight.json").write_text(json.dumps(preflight, indent=2), encoding="utf-8")
    if not args.run:
        print(json.dumps(preflight, indent=2))
        return
    if model.ledger.get("completed") or model.ledger.get("attempts", 0) or model.ledger.get("blocked_auth"):
        raise SystemExit("The independent task ledger already contains attempts; do not reuse it.")
    with claim_path.open("x", encoding="utf-8") as claim:
        json.dump({"batch_id": config["batch_id"], "work": str(work), "result_dir": str(result_dir)}, claim, indent=2)
    (result_dir / "run_config.json").write_text(json.dumps(config | {"model_name": model.config.model_name, "provider_routing": model.provider_statistics(),
         "base_commit": manifest["base_commit"], "mini_commit": "04d809ceab9df28f9adaed044884180159172930"}, indent=2), encoding="utf-8")
    agent = DefaultAgent(
        model, env, system_template="You are a helpful software engineering assistant. Use exactly one bash tool call per response.",
        instance_template=("Fix this issue: {{task}}\n"
            "Work only in this task checkout. Do not access its parent or unrelated projects, browse the web, install packages, or delete files. "
            "Keep temporary helpers in temp/. Do not modify tests/, task_checks/, TASK.md, or evaluation tools. "
            "Inspect relevant code with searches and short line ranges; avoid printing entire large files. "
            "Reproduce the issue, implement a minimal source change, and verify both commands:\n"
            "python -B -m unittest tests.test_more.WindowedTests -v\n"
            "python -B -m unittest discover -s task_checks -v\n"
            "Every command runs in a fresh Git Bash shell on Windows with Python available. "
            "Full-suite timing failures are separately evaluated; do not modify unrelated functionality. "
            "When finished, submit using echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT by itself."),
        step_limit=0, cost_limit=model.budget.agent_cost_limit, wall_time_limit_seconds=config["wall_time_limit_seconds"],
        output_path=result_dir / "trajectory.json",
    )
    start = time.monotonic()
    failure_type = ""
    try:
        status = agent.run(original_issue.decode("utf-8"))
    except Exception as error:
        failure_type = type(error).__name__
        status = {"exit_status": failure_type}
    finally:
        model.ledger["completed"] = True
        model.ledger["note"] = "One real-repository batch ended; do not use unused attempts automatically."
        model.ledger_path.write_text(json.dumps(model.ledger, indent=2), encoding="utf-8")
    elapsed = time.monotonic() - start
    assert old_ledger.read_bytes() == old_ledger_bytes
    assert not changed_inputs(TASK / "repo", manifest)
    assert (TASK / "checks" / "test_issue_462.py").read_bytes() == original_check
    assert (TASK / "issue.md").read_bytes() == original_issue
    verification = evaluate(work, result_dir / "verification.json")
    changed = changed_inputs(work, manifest)
    non_source_changes = [name for name in changed if not name.startswith("more_itertools/")]
    checks_unchanged = (work / "task_checks" / "test_issue_462.py").read_bytes() == original_check
    issue_unchanged = (work / "TASK.md").read_bytes() == original_issue
    patches = []
    for name in changed:
        before = (TASK / "repo" / name).read_text(encoding="utf-8").splitlines(keepends=True) if (TASK / "repo" / name).is_file() else []
        after = (work / name).read_text(encoding="utf-8").splitlines(keepends=True) if (work / name).is_file() else []
        patches.extend(difflib.unified_diff(before, after, fromfile="a/" + name, tofile="b/" + name))
    (result_dir / "repair.diff").write_text("".join(patches), encoding="utf-8")
    before_full = baseline["full_upstream_suite"]
    after_full = verification["full_upstream_suite"]
    new_failures = sorted(set(after_full["failures"] + after_full["errors"]) - set(before_full["failures"] + before_full["errors"]))
    valid = verification["task_tests_passed"] and checks_unchanged and issue_unchanged and not non_source_changes
    summary = {
        "batch_id": config["batch_id"], "task_id": manifest["task_id"], "work": str(work),
        "base_commit": manifest["base_commit"], "exit_status": status.get("exit_status"),
        "failure_type": failure_type, "provider_failure": model.ledger.get("provider_failure"), "requests": model.requests, "usage": model.usage,
        "estimated_cost_cny": model.complete_cost_estimate(), "known_price_estimate_cny": model.actual_cost_cny, "shared_budget": model.budget.snapshot(),
        "provider_routing": model.provider_statistics(),
        "elapsed_seconds": round(elapsed, 3), "changed_files": changed,
        "non_source_changes": non_source_changes, "protected_tests_unchanged": verification["protected_tests_unchanged"],
        "issue_checks_unchanged": checks_unchanged, "issue_description_unchanged": issue_unchanged,
        "old_ledger_unchanged": True, "task_tests_passed": valid,
        "full_suite_tests": after_full["tests_run"], "full_suite_passed": after_full["passed"],
        "full_suite_failures": after_full["failures"], "full_suite_errors": after_full["errors"],
        "new_full_suite_failures": new_failures,
        "repair_verified": valid and not new_failures,
        "scope": "One historical selected Windows task; not SWE-bench or proof of general improvement.",
    }
    (result_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    # Refuse to publish raw content if the configured key appears in any result.
    assert all(not model.contains_credentials(path.read_text(encoding="utf-8")) for path in result_dir.iterdir() if path.is_file())
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("Temporary result: " + str(result_dir))


if __name__ == "__main__":
    main()
