"""Read paired evidence with predeclared metrics; never invoke a model."""
from decimal import Decimal
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
name = "retrieval_delivery_trial_20261015"
config = json.loads((ROOT / "configs" / (name + ".json")).read_text(encoding="utf-8"))
policy = json.loads((ROOT / "configs" / (name + ".metrics.json")).read_text(encoding="utf-8"))
rows = []
for item in config["run_order"]:
    claim = ROOT / "temp" / (name + "_" + item["task"] + "_" + item["variant"] + "_claim.json")
    if not claim.exists():
        continue
    source = Path(json.loads(claim.read_text(encoding="utf-8"))["results"])
    if not (source / "summary.json").exists():
        continue
    data = json.loads((source / "summary.json").read_text(encoding="utf-8"))
    run_config = json.loads((source / "run_config.json").read_text(encoding="utf-8"))
    baseline = json.loads((source / "baseline.json").read_text(encoding="utf-8"))
    verified = json.loads((source / "verification.json").read_text(encoding="utf-8"))
    events = [json.loads(line) for line in (source / "task-state/events.jsonl").read_text(encoding="utf-8").splitlines()]
    prefix = run_config["task_manifest"]["source_prefix"]
    first_edit = next((event["sequence"] for event in events if any(path.startswith(prefix) for path in event.get("changed_files_this_action", []))), None)
    diagnostic = policy["diagnostic_paths_evaluator_only"][item["task"]]
    reads = [event for event in events if event["kind"] == "executed" and event["returncode"] == 0
             and any(path in event["command"].replace("\\", "/") for path in diagnostic)
             and re.search(r"(?:\b(?:cat|sed|grep|rg|head|tail)\b|read_text|read_bytes)", event["command"])]
    usage = data["usage"]
    row = item | {"results": str(source), "exit_status": data["exit_status"], "limit_kind": data.get("limit_kind"),
        "normal_submission": data["exit_status"] == "Submitted", "repair_verified": data["repair_verified"],
        "requests": data["requests"], "successful_model_responses": len(usage),
        "first_implementation_edit_action": first_edit,
        "first_explicit_target_read_action": reads[0]["sequence"] if reads else None,
        "prompt_tokens": sum(item["prompt_tokens"] for item in usage),
        "completion_tokens": sum(item["completion_tokens"] for item in usage),
        "known_price_estimate_cny": float(sum((Decimal(str(item["estimated_cost_cny"])) for item in usage if item.get("estimated_cost_cny") is not None), Decimal(0))),
        "complete_cost_estimate_cny": data["estimated_cost_cny"], "elapsed_seconds": data["elapsed_seconds"],
        "response_models": sorted({u.get("response_model", "unknown") for u in usage}),
        "successful_providers": sorted({u.get("provider", "unknown") for u in usage}),
        "issue_total": verified["issue_checks"]["tests_run"], "issue_passed": verified["issue_checks"]["passed"],
        "targeted_passed": verified["upstream_targeted"]["passed"],
        "extended_total": verified["full_upstream_suite"]["tests_run"], "extended_passed": verified["full_upstream_suite"]["passed"],
        "baseline_extended_failure_ids": sorted(set(baseline["full_upstream_suite"]["failures"] + baseline["full_upstream_suite"]["errors"])),
        "new_extended_failure_ids": data["new_full_suite_failures"], "patch_replay_passed": data["patch_replay_passed"],
        "protected_inputs_unchanged": data["checks_and_description_unchanged"],
        "changed_files": data["changed_files"], "unexpected_changes": data["unexpected_changes"],
        "blocked_reads": data["task_state"]["blocked_reads"],
        "unattempted_request_projections": sum(not measurement["http_attempted"] for measurement in data["request_measurements"])}
    if data["repository_retrieval"] is not None:
        projections = data["repository_retrieval"]["projections"]
        actual = [event for event in projections if not event.get("preview", False)]
        row.update(retrieval_projection_bytes=sum(e["message_utf8_bytes"] for e in actual),
            retrieval_full_projection_bytes=sum(e["full_message_utf8_bytes"] for e in actual),
            full_projections=sum(e["delivery_mode"] == "full" for e in actual),
            reference_projections=sum(e["delivery_mode"] == "reference" for e in actual),
            retrieval_delivery_receipts=len((source / "repository-retrieval/deliveries.jsonl").read_text().splitlines())
                if (source / "repository-retrieval/deliveries.jsonl").exists() else 0)
        initial = projections[0]["selected_hits"]
        row.update(initial_selected_hits=[{k:hit[k] for k in ["path", "symbol", "start"]} for hit in initial],
            initial_contains_diagnostic_file=any(hit["path"] in diagnostic for hit in initial),
            max_retrieval_message_bytes=max(event["message_utf8_bytes"] for event in projections),
            projection_count_including_preflight=len(projections),
            changed_implementation_reindexed=any(any(path.startswith(prefix) for path in event["refreshed_files"]) for event in projections[1:]))
    rows.append(row)
pairs = []
for task in config["tasks"]:
    pair = [row for row in rows if row["task"] == task]
    if len(pair) != 2:
        continue
    before, after = pair
    same = bool(before["response_models"] and before["response_models"] == after["response_models"]
                and before["successful_providers"] == after["successful_providers"])
    pairs.append({"task": task, "same_served_models_and_providers": same,
        "repair_verified_off_on": [before["repair_verified"], after["repair_verified"]],
        "successful_responses_off_on": [before["successful_model_responses"], after["successful_model_responses"]],
        "first_edit_off_on": [before["first_implementation_edit_action"], after["first_implementation_edit_action"]],
        "prompt_tokens_off_on": [before["prompt_tokens"], after["prompt_tokens"]]})
report = {"date": "2026-10-15", "complete": len(rows) == len(config["run_order"]), "rows": rows, "pairs": pairs,
          "scope": config["scope"], "read_metric_limit": policy["first_read_definition"]}
(ROOT / "temp/retrieval_delivery_analysis.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"complete": report["complete"], "pairs": pairs, "completed_runs": len(rows)}, indent=2))
