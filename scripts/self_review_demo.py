"""Offline S+ wire demonstration: real files/checks, scripted transport only.

All model replies and usage are synthetic. No --run/live API option exists.
The public development fixture is not new-task effectiveness evidence.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import shlex
import uuid
from unittest.mock import patch

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["MSWEA_SILENT_STARTUP"] = "1"
from litellm import ModelResponse
from collaboration import CollaborationCoordinator, attributed_usage
from collaboration_live import LiveRunner
from collaboration_panel import public_policy
from collaboration_protocol import canonical_bytes
from collaboration_store import CollaborationStore
from collaboration_diagnostic import OLD, SEEDED, FIXED
from deepseek_budget import SharedBudget, load_budget_config
from task_service_store import ROOT


def response(tool, arguments, content=None):
    return ModelResponse(id="offline-self-review", model="offline-model", choices=[{"index": 0,
        "message": {"role": "assistant", "content": content, "tool_calls": [{"id": "call-" + uuid.uuid4().hex,
            "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments)}}]}, "finish_reason": "tool_calls"}],
        usage={"prompt_tokens": 100, "completion_tokens": 20})


def exercise(*, scenario="revise", attack_bash=False, cancel_phase=None, checkpoint=None):
    if scenario not in {"revise", "approve", "insufficient", "provider_failure"}:
        raise ValueError("Choose a declared offline scenario")
    home = ROOT / "temp/self_review_demo" / uuid.uuid4().hex
    store = CollaborationStore(home)
    ledger = home / "synthetic_ledger.json"
    ledger.write_bytes(canonical_bytes({"historical_estimated_cost_cny": "0.0", "historical_unknown_charge_reservation_cny": "0.0", "blocked": False, "requests": {}}))
    accounting = SharedBudget(load_budget_config(), ledger_path=ledger, temp_dir=home / "locks")
    parent = store.create_collaboration("split-after-boundary", "adaptive_history", uuid.uuid4().hex,
        public_policy(self_review=True), execution="live")
    store.update(parent["id"], transport_scope="scripted_offline", scope="All model replies/usage synthetic; real local files, grader and patch replay. Not real model effectiveness.")
    settings = {"DEEPSEEK_API_KEY": "offline-private-" + uuid.uuid4().hex,
        "DEEPSEEK_API_BASE_URL": "https://api.deepseek.com", "DEEPSEEK_CHAT_MODEL": "deepseek-flash"}
    runner = LiveRunner(store, parent["id"], settings=settings, accounting=accounting)
    runner.setup()
    counts, wires = {}, []
    marker = "OFFLINE_SAME_AUTHOR_" + parent["id"]
    def transport(**kwargs):
        role = next(r for r in store.roles(parent["id"]) if r["status"] == "running")
        phase = role["phase"]
        counts[phase] = counts.get(phase, 0) + 1
        wires.append({"phase": phase, "messages": deepcopy(kwargs["messages"]),
                      "tool_names": [t["function"]["name"] for t in kwargs["tools"]]})
        if role["role"] == "repairer":
            if counts[phase] == 2:
                return response("bash", {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})
            before, after = (OLD, FIXED if scenario == "approve" else SEEDED) if phase == "repairing" else (SEEDED, FIXED)
            code = "from pathlib import Path; p=Path('more_itertools/more.py'); s=p.read_text(); a=s.index('\\ndef split_after('); b=s.index('\\ndef split_when(',a); body=s[a:b]; assert body.count(" + repr(before) + ")==1; p.write_text(s[:a]+body.replace(" + repr(before) + "," + repr(after) + ")+s[b:])"
            if phase == "revising":
                drafts = [{"finding_id": "falsey-remainder", "disposition": "accepted", "reason": "Synthetic correction, independently checked afterwards", "evidence_ids": []}]
                code += "; Path('temp/finding_responses.json').write_text(" + repr(json.dumps(drafts)) + ")"
            return response("bash", {"command": "python -B -c " + shlex.quote(code)}, marker if phase == "repairing" else "OFFLINE_REVISION_HISTORY")
        if scenario == "provider_failure":
            raise RuntimeError("Scripted offline transport failure")
        if attack_bash and counts[phase] == 1:
            return response("bash", {"command": "echo SHOULD_NEVER_EXECUTE"})
        data = next(json.loads(m["content"].split("\n", 1)[1]) for m in reversed(kwargs["messages"])
                    if m["role"] == "user" and m["content"].startswith("Use registered IDs below."))
        read_ordinal = 2 if attack_bash else 1
        if phase == "reviewing" and counts[phase] == read_ordinal:
            doc = next(d for d in data["documents"] if d["side"] == "candidate" and d["path"] == "more_itertools/more.py")
            return response("read_lines", {"snapshot_id": data["snapshot_id"], "document_id": doc["document_id"], "start": 1427, "end": 1433})
        report = {k: data[k] for k in ("run_id", "reviewer_role_run_id", "candidate_id", "source_tree_digest", "review_id")}
        verdict = "request_changes" if phase == "reviewing" and scenario == "revise" else "insufficient_evidence" if scenario == "insufficient" else "approve"
        report.update(verdict=verdict, coverage=["Artificial same-session self-check, not real model reasoning"], findings=[])
        if verdict == "request_changes":
            ev = json.loads(kwargs["messages"][-1]["content"])["evidence"]["evidence_id"]
            report["findings"] = [{"finding_id": "falsey-remainder", "severity": "major", "category": "behavior", "claim": "A nonempty zero remainder should remain.",
                "requirement_ref": "issue:paragraph-4", "impact": "Artificial declared edge-case concern, not a new discovery", "evidence_ids": [ev]}]
        if verdict == "insufficient_evidence":
            report["unknowns"] = ["Scripted evidence gap"]
        return response("submit_review", {"report": report})
    def stage_checkpoint(name, role):
        if checkpoint is not None:
            checkpoint(name, role, runner)
        if name == "after_role_claim" and role["phase"] == cancel_phase:
            store.cancel(parent["id"])
    try:
        with patch("litellm.completion", side_effect=transport):
            result = CollaborationCoordinator(store, runner, checkpoint=stage_checkpoint).run(parent["id"])
        if result["status"] != "cancelled":
            runner.finalize(result)
    finally:
        runner.close()
    (home / "wire.json").write_bytes(canonical_bytes(wires))
    summary = {"scope": "All replies/usage synthetic, real local copies/checks/replay; zero external API calls, no effectiveness claim.",
        "run_id": parent["id"], "scenario": scenario, "counts": counts, "run": store.get(parent["id"]),
        "usage": attributed_usage(accounting, parent["id"]), "history_events": runner.self_session.events if runner.self_session else []}
    (home / "demonstration.json").write_bytes(canonical_bytes(summary))
    return store, runner, summary, wires


if __name__ == "__main__":
    store, runner, summary, _ = exercise(attack_bash=True)
    assert summary["run"]["status"] == "finished" and summary["run"]["repair_result"] == "passed", summary["run"]
    print(json.dumps({"output": str(store.home / "demonstration.json"), "run_id": summary["run_id"],
        "scope": summary["scope"], "counts": summary["counts"]}, ensure_ascii=False))
