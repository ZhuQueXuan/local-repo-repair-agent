"""Protocol rejection, candidate lineage and evidence semantics; no API calls."""
from pathlib import Path
import unittest
import uuid

from pydantic import ValidationError

from collaboration_protocol import (Candidate, EvidenceRef, FindingResponse, Handoff,
    ProgramReport, ProtocolError, ReviewReport, RoleIdentity, canonical_bytes,
    issue_requirements, relative_path, sha256, tree_digest, validate_evidence,
    validate_handoff, validate_response, validate_review)
from review_tools import ROOT, ReviewEvidenceBroker, seal_candidate


class StageAFixture:
    """A synthetic task. Keep each fixture in temp; never recursively clean it."""
    def __init__(self, run_id="run-a"):
        self.root = ROOT / "temp/multi_agent_stage_a_20261022/tests" / uuid.uuid4().hex
        self.baseline = self.root / "baseline"
        self.work = self.root / "work"
        self.issue = b"Empty input must return zero.\n\nNon-empty input must retain its sum.\n"
        self.original = b"def total(values):\n    return sum(values) / len(values)\n"
        self.v1 = b"def total(values):\n    return sum(values)\n"
        self.v2 = b"def total(values):\n    if not values:\n        return 0\n    return sum(values)\n"
        self.sources = {"pkg/main.py": self.original, "tests/test_main.py": b"# protected upstream fixture\n", "docs/rules.md": b"Public behavior must remain compatible.\n"}
        for folder in (self.baseline, self.work):
            for relative, raw in self.sources.items():
                path = folder / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
        (self.work / "TASK.md").write_bytes(self.issue)
        (self.work / "temp").mkdir()
        self.manifest = {"source_prefix": "pkg/", "source_files_sha256": {p: sha256(raw) for p, raw in self.sources.items()}}
        self.run_id = run_id
        self.reviewer = RoleIdentity(run_id=run_id, role_run_id="reviewer-1", session_id="review-session-1", role="reviewer")
        self.write(self.v1)
        self.snapshot = self.seal("v1")
        self.report = ProgramReport(report_id="test-v1", run_id=run_id, candidate_id=self.snapshot.candidate.candidate_id,
                                    source_tree_digest=self.snapshot.candidate.source_tree_digest, producer_role_run_id="verifier-1",
                                    origin="program_test", status="passed", scope="Synthetic offline fixture; not actual evaluator execution", summary="Fixture report for protocol tests")
        self.handoff = self.make_handoff()
        self.broker = ReviewEvidenceBroker(self.snapshot, self.reviewer, self.handoff, reports=(self.report,))

    def write(self, raw):
        (self.work / "pkg/main.py").write_bytes(raw)

    def seal(self, name):
        return seal_candidate(self.manifest, self.baseline, self.work, self.issue, self.root / name, run_id=self.run_id, producer_role_run_id="repairer-1")

    def make_handoff(self, **changes):
        c = self.snapshot.candidate
        fields = dict(handoff_id="handoff-1", run_id=c.run_id, from_role_run_id=c.producer_role_run_id,
                      to_role_run_id=self.reviewer.role_run_id, task_digest=c.task_digest, baseline_digest=c.baseline_digest,
                      candidate_id=c.candidate_id, source_tree_digest=c.source_tree_digest, patch_sha256=c.patch_sha256,
                      snapshot_id=c.snapshot_id, read_document_ids=tuple(d.document_id for d in c.documents),
                      report_ids=(self.report.report_id,), questions=("Check the change against the original task.",))
        return Handoff.parse(fields | changes)

    def doc(self, side="candidate", path="pkg/main.py"):
        return next(doc for doc in self.snapshot.candidate.documents if (doc.side, doc.path) == (side, path))

    def read(self):
        return self.broker.dispatch(self.reviewer, "read_lines", {"snapshot_id": self.snapshot.candidate.snapshot_id,
            "document_id": self.doc().document_id, "start": 1, "end": 2})

    def review(self, **changes):
        ref = self.read()["evidence"]["evidence_id"]
        fields = dict(review_id="review-1", run_id=self.run_id, reviewer_role_run_id=self.reviewer.role_run_id,
                      candidate_id=self.snapshot.candidate.candidate_id, source_tree_digest=self.snapshot.candidate.source_tree_digest,
                      verdict="request_changes", coverage=("Changed function",), findings=({"finding_id": "finding-1", "severity": "major",
                          "category": "behavior", "claim": "Synthetic disputed claim for protocol validation, not a confirmed defect.",
                          "evidence_ids": (ref,), "requirement_ref": "issue:paragraph-1", "impact": "Requires independent confirmation"},))
        return ReviewReport.parse(fields | changes)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.f = StageAFixture()

    def validate(self, review):
        return validate_review(review, self.f.snapshot.candidate, self.f.reviewer, self.f.broker.evidence,
            self.f.broker.documents, self.f.broker.reports, self.f.snapshot.requirements)

    def test_canonical_encoding_is_stable_and_disallows_nan(self):
        self.assertEqual(canonical_bytes({"b": 2, "a": "中文"}), canonical_bytes({"a": "中文", "b": 2}))
        self.assertEqual(tree_digest({"b": "2", "a": "1"}), tree_digest({"a": "1", "b": "2"}))
        with self.assertRaises(ValueError):
            canonical_bytes({"cost": float("nan")})

    def test_portable_paths_reject_traversal_devices_ads_and_aliases(self):
        bad = ["../x", "/x", "C:/x", "C:\\x", "//server/x", "x/../y", "x//y", "x:secret", "CON.txt", "a/NUL", "a.", "a ", "a\x00b"]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                relative_path(value)
        self.assertEqual(relative_path("docs/规则.md"), "docs/规则.md")

    def test_roundtrip_candidate_and_nested_models_are_immutable(self):
        c = self.f.snapshot.candidate
        self.assertEqual(c, Candidate.parse(c.model_dump(mode="json")))
        with self.assertRaises(ValidationError):
            c.documents[0].path = "elsewhere.py"
        self.assertIsInstance(c.documents, tuple)

    def test_unknown_fields_versions_and_scalar_coercion_are_rejected(self):
        for changes in [{"write": True}, {"write": 0}, {"execute": 0}, {"schema_version": 2}, {"schema_version": True}, {"schema_version": 1.0}, {"deadline_seconds": "120"}, {"deadline_seconds": True}, {"shell": "echo bypass"}]:
            with self.subTest(changes=changes), self.assertRaises((ValidationError, ProtocolError)):
                self.f.make_handoff(**changes)

    def test_roles_are_not_limited_to_repairer_and_reviewer(self):
        role = RoleIdentity(run_id="run-a", role_run_id="docs-1", session_id="docs-session", role="document-investigator")
        self.assertEqual(role.role, "document-investigator")

    def test_requirement_ids_come_from_original_issue(self):
        requirements = issue_requirements("One.\n\nTwo.")
        self.assertEqual([r.requirement_id for r in requirements], ["issue:paragraph-1", "issue:paragraph-2"])
        review = self.f.review()
        data = review.model_dump(mode="json")
        data["findings"][0]["requirement_ref"] = "invented:requirement"
        with self.assertRaisesRegex(ProtocolError, "requirement_not_registered"):
            self.validate(ReviewReport.parse(data))

    def test_valid_handoff_and_review_preserve_versions(self):
        self.assertEqual(validate_handoff(self.f.handoff, self.f.snapshot.candidate, self.f.reviewer, self.f.broker.reports, {}), self.f.handoff)
        self.assertEqual(self.validate(self.f.review()).verdict, "request_changes")

    def test_handoff_rejects_wrong_run_recipient_patch_and_document_scope(self):
        for changes in [{"run_id": "another-run"}, {"to_role_run_id": "another-reviewer"}, {"patch_sha256": "0" * 64}, {"read_document_ids": ("unknown-doc",)}, {"report_ids": ("missing-report",)}]:
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                validate_handoff(self.f.make_handoff(**changes), self.f.snapshot.candidate, self.f.reviewer, self.f.broker.reports, {})

    def test_stale_review_cannot_approve_new_candidate(self):
        review = self.f.review()
        self.f.write(self.f.v2)
        newer = self.f.seal("v2")
        with self.assertRaisesRegex(ProtocolError, "version_or_run_mismatch"):
            validate_review(review, newer.candidate, self.f.reviewer, self.f.broker.evidence, self.f.broker.documents,
                            self.f.broker.reports, newer.requirements)

    def test_missing_or_wrong_source_evidence_is_not_actionable(self):
        review = self.f.review()
        data = review.model_dump(mode="json")
        data["findings"][0]["evidence_ids"] = ["invented-evidence"]
        with self.assertRaisesRegex(ProtocolError, "evidence_not_registered"):
            self.validate(ReviewReport.parse(data))
        ref = EvidenceRef.parse(self.f.read()["evidence"])
        for changes in [{"file_sha256": "0" * 64}, {"path": "pkg/other.py"}, {"end_line": 1000}, {"run_id": "run-b"}]:
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                validate_evidence(EvidenceRef.parse(ref.model_dump(mode="json") | changes), self.f.snapshot.candidate, self.f.broker.documents, self.f.broker.reports)

    def test_altered_span_with_plausible_hash_is_not_issued_evidence(self):
        ref = self.f.read()["evidence"]
        with self.assertRaisesRegex(ProtocolError, "evidence_not_issued_or_altered"):
            self.f.broker.validate_issued_evidence(ref | {"span_sha256": "0" * 64})

    def test_model_claim_cannot_become_program_test_or_only_finding_evidence(self):
        claim = ProgramReport.parse(self.f.report.model_dump(mode="json") | {"origin": "model_claim"})
        broker = ReviewEvidenceBroker(self.f.snapshot, self.f.reviewer, self.f.handoff, reports=(claim,))
        ref = broker.dispatch(self.f.reviewer, "read_report", {"report_id": claim.report_id})["evidence"]
        with self.assertRaisesRegex(ProtocolError, "report_origin_or_hash_mismatch"):
            validate_evidence(EvidenceRef.parse(ref | {"kind": "program_test"}), self.f.snapshot.candidate, broker.documents, broker.reports)
        review = self.f.review()
        data = review.model_dump(mode="json")
        data["findings"][0]["evidence_ids"] = [ref["evidence_id"]]
        with self.assertRaisesRegex(ProtocolError, "model_claim_is_not_independent_evidence"):
            validate_review(ReviewReport.parse(data), self.f.snapshot.candidate, self.f.reviewer, broker.evidence, broker.documents, broker.reports, self.f.snapshot.requirements)

    def test_stale_or_missing_program_report_is_rejected(self):
        ref = EvidenceRef.parse(self.f.broker.dispatch(self.f.reviewer, "read_report", {"report_id": self.f.report.report_id})["evidence"])
        with self.assertRaisesRegex(ProtocolError, "report_not_available"):
            validate_evidence(ref, self.f.snapshot.candidate, self.f.broker.documents, {})
        stale = ProgramReport.parse(self.f.report.model_dump(mode="json") | {"candidate_id": "old-candidate"})
        with self.assertRaises(ProtocolError):
            ReviewEvidenceBroker(self.f.snapshot, self.f.reviewer, self.f.handoff, reports=(stale,))

    def test_no_findings_and_style_only_cannot_request_changes(self):
        data = self.f.review().model_dump(mode="json")
        with self.assertRaisesRegex(ValidationError, "request_changes_without_actionable_finding"):
            ReviewReport.parse(data | {"findings": []})
        data["findings"][0].update(category="style", severity="informational")
        with self.assertRaises(ValidationError):
            ReviewReport.parse(data)

    def test_approve_does_not_override_major_findings(self):
        with self.assertRaisesRegex(ValidationError, "approve_with_unresolved_major_finding"):
            self.f.review(verdict="approve")
        self.assertEqual(self.validate(self.f.review(verdict="approve", findings=())).verdict, "approve")

    def test_insufficient_evidence_is_valid_with_an_explicit_gap(self):
        with self.assertRaisesRegex(ValidationError, "missing_evidence_gap"):
            self.f.review(verdict="insufficient_evidence", findings=())
        review = self.f.review(verdict="insufficient_evidence", findings=(), unknowns=("No independent probe for the claimed boundary.",))
        self.assertEqual(self.validate(review).verdict, "insufficient_evidence")

    def response(self, **changes):
        review = self.f.review()
        fields = dict(response_id="response-1", run_id=self.f.run_id, responder_role_run_id="repairer-1", review_id=review.review_id,
                      finding_id="finding-1", reviewed_candidate_id=review.candidate_id, target_candidate_id=review.candidate_id,
                      disposition="needs_evidence", reason="The claimed behavior needs a concrete reproduction.")
        return review, FindingResponse.parse(fields | changes)

    def check_response(self, review, response, target=None, broker=None):
        broker = broker or self.f.broker
        responder = RoleIdentity(run_id=self.f.run_id, role_run_id="repairer-1", session_id="repair-session-1", role="repairer")
        return validate_response(response, review, self.f.snapshot.candidate, target or self.f.snapshot.candidate, responder,
                                 broker.evidence, broker.documents, broker.reports)

    def test_acceptance_is_not_an_independent_resolution(self):
        review, response = self.response(disposition="accepted")
        result = self.check_response(review, response)
        self.assertEqual(result.disposition, "accepted")
        self.assertNotIn("resolved", result.model_dump())

    def test_rejection_needs_evidence_but_unverified_can_stop(self):
        review, response = self.response(disposition="rejected")
        with self.assertRaisesRegex(ProtocolError, "rejection_needs_evidence"):
            self.check_response(review, response)
        ref = self.f.read()["evidence"]["evidence_id"]
        review, response = self.response(disposition="rejected", evidence_ids=(ref,))
        self.assertEqual(self.check_response(review, response).disposition, "rejected")
        review, response = self.response()
        self.assertEqual(self.check_response(review, response).disposition, "needs_evidence")

    def test_response_cannot_use_another_task_candidate_or_unknown_finding(self):
        review, response = self.response(finding_id="not-a-finding")
        with self.assertRaises(ProtocolError):
            self.check_response(review, response)
        other = StageAFixture(run_id="other-run")
        review, response = self.response(target_candidate_id=other.snapshot.candidate.candidate_id)
        with self.assertRaises(ProtocolError):
            self.check_response(review, response, target=other.snapshot.candidate)


if __name__ == "__main__":
    unittest.main(verbosity=2)
