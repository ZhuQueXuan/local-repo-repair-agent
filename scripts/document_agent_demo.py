"""Three-role offline wire demo. Every reply and usage is scripted, never live.

Uses the existing public development task, real files/Bash/checks/patch replay,
and a synthetic temp ledger. There is no live/run API option in this module.
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
from collaboration import CollaborationCoordinator, attributed_usage
from collaboration_live import LiveRunner
from collaboration_panel import public_policy
from collaboration_protocol import canonical_bytes
from collaboration_store import CollaborationStore
from collaboration_diagnostic import OLD, SEEDED, FIXED
from deepseek_budget import SharedBudget, load_budget_config
from self_review_demo import response
from task_service_store import ROOT


def exercise(*, scenario="revise", attack_bash=True, cancel_phase=None, checkpoint=None, question=None, task_card=None, allow_pause=False, allow_capabilities=False, model_profile=None, continuation=None):
    if scenario not in {"revise", "approve", "not_found", "provider_failure"}:
        raise ValueError("Choose a declared offline scenario")
    home = Path(continuation["home"]) if continuation else ROOT / "temp/document_agent_demo" / uuid.uuid4().hex
    store = CollaborationStore(home)
    ledger = home / "synthetic_ledger.json"
    if not continuation:
        ledger.write_bytes(canonical_bytes({"historical_estimated_cost_cny": "0.0", "historical_unknown_charge_reservation_cny": "0.0", "blocked": False, "requests": {}}))
    accounting = SharedBudget(load_budget_config(), ledger_path=ledger, temp_dir=home / "locks")
    parent = store.get(continuation["run_id"]) if continuation else store.create_collaboration("split-after-boundary", "adaptive_history", uuid.uuid4().hex,
        public_policy(documents=True, question=question or "What does the frozen repository README say this library provides, and what remains unspecified about the reported boundary?"), execution="live", task_card=task_card, allow_pause=allow_pause, allow_capabilities=allow_capabilities, model_profile=model_profile)
    store.update(parent["id"], transport_scope="scripted_offline", scope="Three-role scripted transport and usage; real local files/checks/replay. Not real model or new-task effectiveness.")
    settings = {"DEEPSEEK_API_KEY": "offline-private-" + uuid.uuid4().hex,
        "DEEPSEEK_API_BASE_URL": "https://api.deepseek.com", "DEEPSEEK_CHAT_MODEL": "deepseek-flash"}
    runner = LiveRunner(store, parent["id"], settings=settings, accounting=accounting)
    runner.setup()
    counts, wires, ordinary_counts = {}, [], {}
    def transport(**kwargs):
        role = next(r for r in store.roles(parent["id"]) if r["status"] == "running")
        phase = role["phase"]
        counts[phase] = counts.get(phase, 0) + 1
        wires.append({"phase": phase, "messages": deepcopy(kwargs["messages"]),
            "tool_names": [t["function"]["name"] for t in kwargs["tools"]]})
        if parent.get("harness_capabilities_version") == 1 and phase in {"consulting", "reviewing", "final_review"}:
            prefix = "Use registered document IDs below." if phase == "consulting" else "Use registered IDs below."
            info = next(json.loads(m["content"].split("\n", 1)[1]) for m in reversed(kwargs["messages"])
                        if m["role"] == "user" and m["content"].startswith(prefix))
            skill = info["capability_catalog"]["skills"][0]
            observations = [json.loads(m["content"]) for m in kwargs["messages"] if m["role"] == "tool"]
            if not any(o.get("name") == skill["name"] for o in observations):
                return response("read_skill", {"skill": skill["name"], "sha256": skill["sha256"]})
            if phase != "consulting" and not any("changed_files" in o for o in observations):
                return response("read_change_summary", {"candidate_id": info["candidate_id"]})
        ordinary_counts[phase] = ordinary_counts.get(phase, 0) + 1
        if phase == "consulting":
            if scenario == "provider_failure":
                raise RuntimeError("Scripted document transport failure")
            if attack_bash and ordinary_counts[phase] == 1:
                return response("bash", {"command": "echo DOCUMENT_BASH_MUST_NEVER_EXECUTE"})
            data = json.loads(kwargs["messages"][1]["content"].split("\n", 1)[1])
            grant = data["grant"]
            reads = [json.loads(m["content"])["evidence"]["evidence_id"] for m in kwargs["messages"]
                if m["role"] == "tool" and json.loads(m["content"]).get("evidence")]
            if not reads and scenario != "not_found":
                doc = next(d for d in grant["documents"] if d["line_count"] > 0)
                return response("read_lines", {"snapshot_id": grant["snapshot_id"], "document_id": doc["document_id"], "start": 1, "end": min(doc["line_count"], 8)})
            advice = {k: grant[k] for k in ("run_id", "role_run_id", "snapshot_id", "task_digest", "document_tree_digest")}
            advice.update(advice_id=data["advice_id"], status="information_not_found" if scenario == "not_found" else "answered",
                answer="Synthetic consultation: README describes the library; exact boundary behavior is not established by this excerpt.",
                evidence_ids=reads, unknowns=["Boundary semantics still require code inspection and independent checks."])
            return response("submit_consultation", {"advice": advice})
        if role["role"] == "repairer":
            if ordinary_counts[phase] == 2:
                return response("bash", {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})
            before, after = (OLD, FIXED if scenario == "approve" else SEEDED) if phase == "repairing" else (SEEDED, FIXED)
            code = "from pathlib import Path; p=Path('more_itertools/more.py'); s=p.read_text(); a=s.index('\\ndef split_after('); b=s.index('\\ndef split_when(',a); body=s[a:b]; assert body.count(" + repr(before) + ")==1; p.write_text(s[:a]+body.replace(" + repr(before) + "," + repr(after) + ")+s[b:])"
            if phase == "revising":
                code += "; Path('temp/finding_responses.json').write_text(" + repr(json.dumps([{"finding_id": "falsey-remainder", "disposition": "accepted", "reason": "Scripted revision, not model discovery", "evidence_ids": []}])) + ")"
            return response("bash", {"command": "python -B -c " + shlex.quote(code)}, "OFFLINE_REPAIR_CONTEXT")
        data = next(json.loads(m["content"].split("\n", 1)[1]) for m in reversed(kwargs["messages"])
            if m["role"] == "user" and m["content"].startswith("Use registered IDs below."))
        if phase == "reviewing" and scenario != "approve" and ordinary_counts[phase] == 1:
            doc = next(d for d in data["documents"] if d["side"] == "candidate" and d["path"] == "more_itertools/more.py")
            return response("read_lines", {"snapshot_id": data["snapshot_id"], "document_id": doc["document_id"], "start": 1427, "end": 1433})
        report = {k: data[k] for k in ("run_id", "reviewer_role_run_id", "candidate_id", "source_tree_digest", "review_id")}
        report.update(verdict="request_changes" if phase == "reviewing" and scenario != "approve" else "approve",
            coverage=["Scripted review; documentation interpretation is not defect truth"], findings=[])
        if report["verdict"] == "request_changes":
            # A current human task-card view may follow the latest tool result.
            ev = next(json.loads(m["content"])["evidence"]["evidence_id"] for m in reversed(kwargs["messages"])
                if m.get("role") == "tool" and json.loads(m["content"]).get("evidence"))
            report["findings"] = [{"finding_id": "falsey-remainder", "severity": "major", "category": "behavior", "claim": "A nonempty zero remainder should remain.",
                "requirement_ref": "issue:paragraph-4", "impact": "Artificial known edge case; not a new discovery", "evidence_ids": [ev]}]
        return response("submit_review", {"report": report})
    def stage_checkpoint(name, role):
        if checkpoint is not None:
            checkpoint(name, role, runner)
        if name == "after_role_claim" and role["phase"] == cancel_phase:
            store.cancel(parent["id"])
    try:
        with patch("litellm.completion", side_effect=transport):
            result = CollaborationCoordinator(store, runner, checkpoint=stage_checkpoint).run(parent["id"])
        if result["status"] not in {"cancelled", "paused"}:
            runner.finalize(result)
    finally:
        runner.close()
    (home / ("wire-" + parent["id"] + ".json")).write_bytes(canonical_bytes(wires))
    if not continuation:
        (home / "wire.json").write_bytes(canonical_bytes(wires))
    summary = {"scope": "All replies/usage synthetic; real files/checks/clean replay. Zero external model calls; no effectiveness claim.",
        "run_id": parent["id"], "scenario": scenario, "counts": counts, "run": store.get(parent["id"]),
        "usage": attributed_usage(accounting, parent["id"])}
    (home / ("demonstration-" + parent["id"] + ".json")).write_bytes(canonical_bytes(summary))
    if not continuation:
        (home / "demonstration.json").write_bytes(canonical_bytes(summary))
    return store, runner, summary, wires


if __name__ == "__main__":
    store, runner, summary, _ = exercise()
    assert summary["run"]["status"] == "finished" and summary["run"]["repair_result"] == "passed", summary["run"]
    print(json.dumps({"output": str(store.home / "demonstration.json"), "run_id": summary["run_id"], "counts": summary["counts"], "scope": summary["scope"]}, ensure_ascii=False))
