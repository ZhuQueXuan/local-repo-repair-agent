"""Recover post-run collection only, preserving original patch and paid evidence."""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from task_workspace import prepare_task, changed_files, load_task, digest
from run_task_batch import evaluate
from evaluation_freeze import verify_freeze
from dotenv import dotenv_values
from provider_priority import load_providers

parser = argparse.ArgumentParser()
parser.add_argument("--variant", required=True, choices=["adaptive_history", "repository_retrieval"])
args = parser.parse_args()
freeze = verify_freeze(ROOT, "configs/repository_retrieval_dev_20261014.freeze.json")
claim = json.loads((ROOT / "temp" / ("repository_retrieval_dev_20261014_requests-json-pickle_" + args.variant + "_claim.json")).read_text(encoding="utf-8"))
result, work = Path(claim["results"]), Path(claim["work"])
assert not (result / "summary.json").exists(), "Do not replace completed evidence"
trajectory = json.loads((result / "trajectory.json").read_text(encoding="utf-8"))
ledger = json.loads((result / "ledger.json").read_text(encoding="utf-8"))
assert ledger["completed"]
config = json.loads((result / "run_config.json").read_text(encoding="utf-8"))
baseline = json.loads((result / "baseline.json").read_text(encoding="utf-8"))
verification = json.loads((result / "verification.json").read_text(encoding="utf-8"))
task, manifest = load_task("requests-json-pickle")
changed = changed_files(work, manifest)
unexpected = [name for name in changed if not name.startswith(manifest["source_prefix"])]
protected = all(digest(work / ("TASK.md" if name == "issue.md" else "task_checks/test_behavior.py")) == expected
                for name, expected in manifest["task_files_sha256"].items())
raw = (result / "repair.diff").read_bytes()
fixed = result / "repair_lf.diff"
fixed.write_bytes(raw.replace(b"\r\n", b"\n"))
before_full, after_full = baseline["full_upstream_suite"], verification["full_upstream_suite"]
new_failures = sorted(set(after_full["failures"] + after_full["errors"]) - set(before_full["failures"] + before_full["errors"]))
assert raw and protected and not unexpected and verification["task_tests_passed"] and not new_failures
replay, _ = prepare_task("requests-json-pickle")
subprocess.run(["git", "-C", str(replay), "apply", "--check", "--", str(fixed)], check=True, capture_output=True)
subprocess.run(["git", "-C", str(replay), "apply", "--", str(fixed)], check=True, capture_output=True)
replayed = evaluate("requests-json-pickle", replay, result / "patch_verification.json")
replay_new = set(replayed["full_upstream_suite"]["failures"] + replayed["full_upstream_suite"]["errors"]) - set(before_full["failures"] + before_full["errors"])
passed = replayed["task_tests_passed"] and not replay_new
info = trajectory["info"]
usage = info["model_budget"]["usage"]
known = sum(Decimal(str(item["estimated_cost_cny"])) for item in usage if item.get("estimated_cost_cny") is not None)
events = [json.loads(line) for line in (work / "temp/task-state/events.jsonl").read_text(encoding="utf-8").splitlines()]
first_edit = next((event["sequence"] for event in events if any(name.startswith(manifest["source_prefix"]) for name in event.get("changed_files_this_action", []))), None)
times = [message["extra"]["timestamp"] for message in trajectory["messages"] if "timestamp" in message.get("extra", {})]
summary = {"task_id": "requests-json-pickle", "variant": args.variant, "exit_status": info["exit_status"],
    "provider_failure": ledger.get("provider_failure"), "provider_routing": info["model_budget"]["provider_routing"],
    "work": str(work), "requests": ledger["attempts"], "successful_model_responses": len(usage),
    "estimated_cost_cny": info["model_budget"]["estimated_actual_cost_cny"], "known_price_estimate_cny": float(known),
    "usage": usage, "request_measurements": None, "context_projections": None, "pinned_state_requests": None,
    "tool_output_statistics": info["tool_output_budget"], "task_state": json.loads((work / "temp/task-state/state.json").read_text(encoding="utf-8")),
    "first_implementation_edit_action": first_edit, "elapsed_seconds": None,
    "first_to_last_response_seconds": round(times[-1] - times[0], 3) if times else None,
    "changed_files": changed, "unexpected_changes": unexpected, "checks_and_description_unchanged": protected,
    "task_tests_passed": verification["task_tests_passed"], "new_full_suite_failures": new_failures,
    "patch_replay_passed": passed, "repair_verified": passed, "scope": manifest["scope"],
    "evaluation_freeze": freeze, "collection_recovered_offline": True,
    "collection_note": "Original CRLF repair.diff preserved. repair_lf.diff changes only patch line endings; applies to fresh baseline and passes independent evaluation. No model rerun or implementation edits. In-memory measurements unavailable after collector crash are null, not reconstructed as exact values."}
if config.get("repository_retrieval") is not None:
    projections = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((work / "temp/repository-retrieval").glob("*.json"))]
    summary["repository_retrieval"] = {"policy": config["repository_retrieval"], "projections": projections}
else:
    summary["repository_retrieval"] = None
for dirname in ["tool-output", "context-history", "task-state", "repository-retrieval"]:
    source = work / "temp" / dirname
    target = result / dirname
    if source.exists() and not target.exists():
        shutil.copytree(source, target)
providers = load_providers(dotenv_values(ROOT / ".env", interpolate=False))
encoded = json.dumps(summary, ensure_ascii=False, indent=2)
assert not any(provider.api_key in encoded for provider in providers)
(result / "summary.json").write_text(encoded, encoding="utf-8")
print(json.dumps({key: summary[key] for key in ["variant", "requests", "successful_model_responses", "exit_status", "repair_verified", "first_implementation_edit_action", "known_price_estimate_cny", "first_to_last_response_seconds"]}, indent=2))
