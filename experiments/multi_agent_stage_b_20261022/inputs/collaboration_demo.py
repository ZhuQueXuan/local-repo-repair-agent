"""Offline stage-A demonstration. Reviews are scripted, tests actually execute.

This is a small synthetic arithmetic task, not an LLM or benchmark result.
All generated files remain under project temp; nothing is deleted.
"""
import json
from pathlib import Path
import subprocess
import sys
import uuid

from collaboration_protocol import (FindingResponse, Handoff, ProgramReport, ProtocolError,
    ReviewReport, RoleIdentity, canonical_bytes, sha256, validate_response, validate_review)
from review_tools import ROOT, ReviewEvidenceBroker, seal_candidate

ISSUE = b"Empty input must return zero.\n\nNon-empty input must return its sum, including negative totals.\n"
BASE = b"def total(values):\n    return sum(values) / len(values)\n"
FIRST = b"def total(values):\n    return abs(sum(values)) if values else 0\n"
FINAL = b"def total(values):\n    return sum(values)\n"


def independent_check(work):
    # Program-owned checks, outside the candidate source. Executed in a separate
    # process against this explicitly synthetic fixture, not arbitrary uploads.
    code = """import json,runpy,sys
fn=runpy.run_path(sys.argv[1])['total']
rows=[]
for values,expected in [([],0),([1,2],3),([-2,1],-1)]:
    try:
        actual=fn(values)
        rows.append({'input':values,'expected':expected,'actual':actual,'passed':actual==expected})
    except Exception as error:
        rows.append({'input':values,'expected':expected,'exception':type(error).__name__,'passed':False})
print(json.dumps(rows))
sys.exit(0 if all(row['passed'] for row in rows) else 1)
"""
    process = subprocess.run([sys.executable, "-B", "-c", code, str(work / "pkg/main.py")], capture_output=True, text=True, encoding="utf-8", timeout=15)
    rows = json.loads(process.stdout)
    if process.returncode != (0 if all(row["passed"] for row in rows) else 1):
        raise RuntimeError("Synthetic verifier exit/report mismatch")
    return rows


def broker_for(snapshot, role_run_id, report):
    candidate = snapshot.candidate
    role = RoleIdentity(run_id=candidate.run_id, role_run_id=role_run_id, session_id=role_run_id + "-session", role="reviewer")
    handoff = Handoff(handoff_id=role_run_id + "-handoff", run_id=candidate.run_id,
        from_role_run_id=candidate.producer_role_run_id, to_role_run_id=role.role_run_id,
        task_digest=candidate.task_digest, baseline_digest=candidate.baseline_digest,
        candidate_id=candidate.candidate_id, source_tree_digest=candidate.source_tree_digest,
        patch_sha256=candidate.patch_sha256, snapshot_id=candidate.snapshot_id,
        read_document_ids=tuple(doc.document_id for doc in candidate.documents), report_ids=(report.report_id,),
        questions=("Check the original arithmetic requirements; cite exact evidence.",))
    return ReviewEvidenceBroker(snapshot, role, handoff, reports=(report,)), handoff


def make_report(snapshot, report_id, rows):
    c = snapshot.candidate
    return ProgramReport(report_id=report_id, run_id=c.run_id, candidate_id=c.candidate_id,
        source_tree_digest=c.source_tree_digest, producer_role_run_id="synthetic-verifier",
        origin="program_test", status="passed" if all(row["passed"] for row in rows) else "failed",
        scope="Three executed synthetic arithmetic cases; not an LLM, real repository or benchmark evaluation.",
        summary=json.dumps(rows, ensure_ascii=False))


def run_demo():
    output = ROOT / "temp/multi_agent_stage_a_20261022" / ("demo_" + uuid.uuid4().hex)
    baseline, work = output / "baseline", output / "work"
    for directory in (baseline, work):
        (directory / "pkg").mkdir(parents=True)
        (directory / "pkg/main.py").write_bytes(BASE)
    (work / "TASK.md").write_bytes(ISSUE)
    manifest = {"source_prefix": "pkg/", "source_files_sha256": {"pkg/main.py": sha256(BASE)}}
    run_id = "synthetic-" + uuid.uuid4().hex
    baseline_rows = independent_check(work)
    (work / "pkg/main.py").write_bytes(FIRST)
    snapshot1 = seal_candidate(manifest, baseline, work, ISSUE, output / "v1", run_id=run_id, producer_role_run_id="repairer-initial")
    rows1 = independent_check(work)
    report1 = make_report(snapshot1, "program-v1", rows1)
    broker1, handoff1 = broker_for(snapshot1, "reviewer-initial", report1)
    doc = next(d for d in snapshot1.candidate.documents if d.side == "candidate" and d.path == "pkg/main.py")
    read1 = broker1.dispatch(broker1.reviewer, "read_lines", {"snapshot_id": snapshot1.candidate.snapshot_id, "document_id": doc.document_id, "start": 1, "end": 2})
    test1 = broker1.dispatch(broker1.reviewer, "read_report", {"report_id": report1.report_id})
    review1 = ReviewReport.parse({"review_id": "scripted-review-v1", "run_id": run_id, "reviewer_role_run_id": broker1.reviewer.role_run_id,
        "candidate_id": snapshot1.candidate.candidate_id, "source_tree_digest": snapshot1.candidate.source_tree_digest,
        "verdict": "request_changes", "coverage": ["Synthetic negative-total case"], "findings": [{"finding_id": "negative-total",
        "severity": "major", "category": "behavior", "claim": "abs() changes the required negative total into a positive number.",
        "evidence_ids": [read1["evidence"]["evidence_id"], test1["evidence"]["evidence_id"]], "requirement_ref": "issue:paragraph-2",
        "impact": "Input [-2, 1] yields 1 instead of -1.", "suggested_validation": {"input": "[-2, 1]", "expected": "-1"}}]})
    broker1.dispatch(broker1.reviewer, "submit_review", {"report": review1.model_dump(mode="json")})
    denied = False
    try:
        broker1.dispatch(broker1.reviewer, "bash", {"command": "echo forbidden"})
    except ProtocolError:
        denied = True
    (work / "pkg/main.py").write_bytes(FINAL)
    snapshot2 = seal_candidate(manifest, baseline, work, ISSUE, output / "v2", run_id=run_id, producer_role_run_id="repairer-revision")
    rows2 = independent_check(work)
    report2 = make_report(snapshot2, "program-v2", rows2)
    broker2, handoff2 = broker_for(snapshot2, "reviewer-final", report2)
    test2 = broker2.dispatch(broker2.reviewer, "read_report", {"report_id": report2.report_id})
    response = FindingResponse(response_id="scripted-acceptance", run_id=run_id, responder_role_run_id="repairer-revision",
        review_id=review1.review_id, finding_id="negative-total", reviewed_candidate_id=snapshot1.candidate.candidate_id,
        target_candidate_id=snapshot2.candidate.candidate_id, disposition="accepted", reason="Removed abs(); the executed cases now pass.",
        evidence_ids=(test2["evidence"]["evidence_id"],))
    repairer = RoleIdentity(run_id=run_id, role_run_id="repairer-revision", session_id="repairer-session", role="repairer")
    validate_response(response, review1, snapshot1.candidate, snapshot2.candidate, repairer, broker2.evidence, broker2.documents, broker2.reports)
    stale_rejected = False
    try:
        validate_review(review1, snapshot2.candidate, broker2.reviewer, broker1.evidence, broker1.documents, broker1.reports, snapshot2.requirements)
    except ProtocolError:
        stale_rejected = True
    review2 = ReviewReport(review_id="scripted-review-v2", run_id=run_id, reviewer_role_run_id=broker2.reviewer.role_run_id,
        candidate_id=snapshot2.candidate.candidate_id, source_tree_digest=snapshot2.candidate.source_tree_digest,
        verdict="approve", coverage=("The same three executed synthetic cases",))
    broker2.dispatch(broker2.reviewer, "submit_review", {"report": review2.model_dump(mode="json")})
    artifacts = {"handoff-v1.json": handoff1, "handoff-v2.json": handoff2, "report-v1.json": report1, "report-v2.json": report2,
        "review-v1.json": review1, "review-v2.json": review2, "response.json": response,
        "evidence-v1.json": [r.model_dump(mode="json") for r in broker1.evidence.values()],
        "evidence-v2.json": [r.model_dump(mode="json") for r in broker2.evidence.values()],
        "events-v1.json": broker1.events, "events-v2.json": broker2.events}
    for name, content in artifacts.items():
        (output / name).write_bytes(canonical_bytes(content))
    summary = {"scope": "Offline synthetic mechanism demonstration. Reviews and source revisions are scripted; test cases execute in separate Python processes.",
        "run_id": run_id, "baseline_passed": sum(r["passed"] for r in baseline_rows), "v1_passed": sum(r["passed"] for r in rows1),
        "v2_passed": sum(r["passed"] for r in rows2), "checks_per_version": 3, "bash_denied": denied,
        "stale_review_rejected": stale_rejected, "v1_stays_unchanged": snapshot1.read(doc.document_id) == FIRST,
        "model_requests": 0, "model_effectiveness_demonstrated": False,
        "candidate_v1": snapshot1.candidate.candidate_id, "candidate_v2": snapshot2.candidate.candidate_id}
    (output / "summary.json").write_bytes(canonical_bytes(summary))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("Evidence: " + str(output / "summary.json"))
    return output


if __name__ == "__main__":
    run_demo()
