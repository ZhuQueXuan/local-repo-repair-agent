"""Persistent offline collaboration example: scripted roles, synthetic accounting.

No model/client/network. Candidate checks execute on a known synthetic fixture.
"""
import json
from pathlib import Path
import uuid

from collaboration import CollaborationCoordinator, RoleBudget, attributed_usage
from collaboration_demo import BASE, FIRST, FINAL, ISSUE, independent_check
from collaboration_protocol import (FindingResponse, Handoff, ProgramReport, ReviewReport,
                                     canonical_bytes, sha256)
from collaboration_store import CollaborationStore
from deepseek_budget import SharedBudget, load_budget_config
from review_tools import ROOT, ReviewEvidenceBroker, seal_candidate


class ScriptedRunner:
    def __init__(self, store, *, verdict="request_changes", reject=False, final_verdict="approve", accounting=False):
        self.store, self.verdict, self.reject, self.final_verdict = store, verdict, reject, final_verdict
        self.accounting = accounting
        self.calls = []
        self.ledger_path = store.home / "synthetic_ledger.json"
        self.config = dict(load_budget_config(), max_total_cost_cny=None)
        if accounting and not self.ledger_path.exists():
            self.ledger_path.write_bytes(canonical_bytes({"historical_estimated_cost_cny": "0", "historical_unknown_charge_reservation_cny": "0", "blocked": False, "requests": {}}))

    def _mock_receipt(self, context):
        request = self.store.prepare_request(context.role["id"], "scripted-query", sha256(b"synthetic request"))
        budget = RoleBudget(self.config, self.store, context.identity, request["id"], ledger_path=self.ledger_path, temp_dir=self.store.home / "accounting-temp")
        ticket = budget.reserve(provider="synthetic")
        budget.before_dispatch(ticket)
        # Deliberately invented receipt, labelled synthetic in this demo. No API.
        budget.settle(ticket, "0.001", usage={"prompt_tokens": 10, "completion_tokens": 2, "response_model": "synthetic-no-model"})
        budget.receipt_recorded(ticket)

    def __call__(self, context):
        context.check()
        role, run_id = context.role, context.role["run_id"]
        self.calls.append(role["phase"])
        if self.accounting:
            self._mock_receipt(context)
        stage = self.store.home / "fixture" / run_id
        baseline, work = stage / "baseline", stage / "work"
        if role["phase"] == "repairing":
            for path in (baseline, work):
                (path / "pkg").mkdir(parents=True)
                (path / "pkg/main.py").write_bytes(BASE)
            (work / "TASK.md").write_bytes(ISSUE)
        meta = self.store.state(run_id)["collaboration_state"]
        if role["phase"] in {"repairing", "revising"}:
            previous_id = meta["candidate_id"]
            if role["phase"] == "repairing" or not self.reject:
                (work / "pkg/main.py").write_bytes(FIRST if role["phase"] == "repairing" else FINAL)
                manifest = {"source_prefix": "pkg/", "source_files_sha256": {"pkg/main.py": sha256(BASE)}}
                snapshot = seal_candidate(manifest, baseline, work, ISSUE, stage / role["phase"], run_id=run_id, producer_role_run_id=role["id"])
                self.store.register_snapshot(role["id"], snapshot)
                rows = independent_check(work)
                report = ProgramReport(report_id=role["phase"] + "-program", run_id=run_id, candidate_id=snapshot.candidate.candidate_id,
                    source_tree_digest=snapshot.candidate.source_tree_digest, producer_role_run_id="synthetic-program-verifier",
                    origin="program_test", status="passed" if all(row["passed"] for row in rows) else "failed",
                    scope="Three synthetic arithmetic cases, executed independently; not production acceptance.", summary=json.dumps(rows))
                self.store.register_record(role["id"], "report", report)
                candidate_id = snapshot.candidate.candidate_id
            else:
                candidate_id = previous_id
            response_ids = []
            if role["phase"] == "revising":
                review = self.store.records(run_id, "review")[meta["review_id"]]
                # Use a registered program-report reference on the target version.
                snapshot = self.store.snapshot(run_id, candidate_id)
                reports = [r for r in self.store.records(run_id, "report").values() if r.candidate_id == candidate_id]
                report = reports[-1]
                fields = dict(run_id=run_id, candidate_id=candidate_id, source_tree_digest=snapshot.candidate.source_tree_digest,
                              producer_role_run_id=role["id"], producer_event_seq=1, kind="program_test", report_id=report.report_id,
                              report_sha256=sha256(canonical_bytes(report)))
                self.store.register_record(role["id"], "evidence", fields | {"evidence_id": "revision-evidence"})
                for finding in review.findings:
                    response = FindingResponse(response_id="response-" + finding.finding_id, run_id=run_id, responder_role_run_id=role["id"],
                        review_id=review.review_id, finding_id=finding.finding_id, reviewed_candidate_id=review.candidate_id,
                        target_candidate_id=candidate_id, disposition="rejected" if self.reject else "accepted",
                        reason="Scripted rejection, not independent proof of a false positive." if self.reject else "Scripted correction; see executed synthetic checks.",
                        evidence_ids=("revision-evidence",))
                    self.store.register_record(role["id"], "response", response)
                    response_ids.append(response.response_id)
            return {"candidate_id": candidate_id, "response_ids": response_ids, "exit_reason": "ScriptedOutput"}
        snapshot = self.store.snapshot(run_id, meta["candidate_id"])
        c = snapshot.candidate
        reports = [r for r in self.store.records(run_id, "report").values() if r.candidate_id == c.candidate_id]
        handoff = Handoff(handoff_id=role["phase"] + "-handoff", run_id=run_id, from_role_run_id=c.producer_role_run_id,
            to_role_run_id=role["id"], task_digest=c.task_digest, baseline_digest=c.baseline_digest, candidate_id=c.candidate_id,
            source_tree_digest=c.source_tree_digest, patch_sha256=c.patch_sha256, snapshot_id=c.snapshot_id,
            read_document_ids=tuple(d.document_id for d in c.documents), report_ids=tuple(r.report_id for r in reports),
            questions=("Check synthetic task behavior and cite exact sources.",))
        self.store.register_record(role["id"], "handoff", handoff)
        broker = ReviewEvidenceBroker(snapshot, context.identity, handoff, reports=reports, cancelled=lambda: self.store.get(run_id)["cancel_requested"])
        doc = next(d for d in c.documents if d.side == "candidate" and d.path == "pkg/main.py")
        read = broker.dispatch(context.identity, "read_lines", {"snapshot_id": c.snapshot_id, "document_id": doc.document_id, "start": 1, "end": doc.line_count})
        verdict = self.verdict if role["phase"] == "reviewing" else self.final_verdict
        findings = [] if verdict != "request_changes" else [{"finding_id": role["phase"] + "-finding", "severity": "major", "category": "behavior",
            "claim": "Scripted negative-total concern, to be checked against program evidence.", "evidence_ids": [read["evidence"]["evidence_id"]],
            "requirement_ref": "issue:paragraph-2", "impact": "The expected signed total must be preserved."}]
        review = ReviewReport.parse({"review_id": role["phase"] + "-review", "run_id": run_id, "reviewer_role_run_id": role["id"],
            "candidate_id": c.candidate_id, "source_tree_digest": c.source_tree_digest, "verdict": verdict, "findings": findings,
            "coverage": ["Synthetic arithmetic function"], "unknowns": ["Scripted evidence gap"] if verdict == "insufficient_evidence" else []})
        broker.dispatch(context.identity, "submit_review", {"report": review.model_dump(mode="json")})
        for ref in broker.evidence.values():
            self.store.register_record(role["id"], "evidence", ref)
        self.store.register_record(role["id"], "review", review)
        return {"review_id": review.review_id, "exit_reason": "ScriptedOutput"}


def run_demo():
    home = ROOT / "temp/multi_agent_stage_b_20261022" / ("demo_" + uuid.uuid4().hex)
    store = CollaborationStore(home)
    parent = store.create_collaboration("synthetic-arithmetic", "raw", "demo", {"schema_version": 1, "feedback_windows": 1, "scope": "synthetic offline demo"},
        input_manifest={"source_prefix": "pkg/", "source_files_sha256": {"pkg/main.py": sha256(BASE)}}, input_issue=ISSUE)
    runner = ScriptedRunner(store, accounting=True)
    result = CollaborationCoordinator(store, runner).run(parent["id"])
    budget = SharedBudget(runner.config, ledger_path=runner.ledger_path, temp_dir=home / "accounting-temp")
    reopened = CollaborationStore(home)
    calls_before = len(runner.calls)
    CollaborationCoordinator(reopened, runner).run(parent["id"])
    summary = {"scope": "Scripted offline orchestration and invented receipts; no model/API calls.", "run_id": parent["id"],
               "status": result["status"], "repair_result": result["repair_result"], "review_verdict": result["review_verdict"],
               "roles": reopened.roles(parent["id"]), "usage": attributed_usage(budget, parent["id"]),
               "restart_did_not_rerun": calls_before == len(runner.calls), "real_model_requests": 0}
    (home / "summary.json").write_bytes(canonical_bytes(summary))
    print(json.dumps({"status": result["status"], "roles": runner.calls, "restart_did_not_rerun": summary["restart_did_not_rerun"],
                      "synthetic_attempts": summary["usage"]["attempts"], "real_model_requests": 0}, ensure_ascii=False))
    print("Evidence: " + str(home / "summary.json"))
    return home


if __name__ == "__main__":
    run_demo()
