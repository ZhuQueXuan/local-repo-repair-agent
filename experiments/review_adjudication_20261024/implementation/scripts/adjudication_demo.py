"""One local fixture for independent decisions; no model, production DB or fees."""
import json
from pathlib import Path
import uuid

from collaboration_protocol import (ReviewReport, RoleIdentity, canonical_bytes, sha256)
from review_tools import ROOT, seal_candidate, ReviewEvidenceBroker
from collaboration_protocol import Handoff
from review_adjudication import OfflineAdjudicator, ProbePlan, FindingBinding, decision_summary

PROBE = '''import unittest
from pkg.calc import total

class Behavior(unittest.TestCase):
    def verify(self, values, expected):
        try:
            actual = total(values)
        except Exception as error:
            self.fail("Declared input raised " + type(error).__name__)
        self.assertEqual(actual, expected)

    def test_empty(self): self.verify([], 0)
    def test_negative(self): self.verify([-3, -2], -5)
    def test_positive(self): self.verify([2, 3], 5)
'''


class AdjudicationFixture:
    def __init__(self, *, visibility="not_previously_reported"):
        self.home = ROOT / "temp/review_adjudication_20261024/fixtures" / uuid.uuid4().hex
        self.home.mkdir(parents=True)
        self.baseline, self.work = self.home / "baseline", self.home / "work"
        self.issue = b"Empty input must return zero; nonempty input must return its sum.\n"
        self.original = b"def total(values):\n    return sum(values) / len(values)\n"
        self.v1 = b"def total(values):\n    return max(0, sum(values))\n"
        self.good = b"def total(values):\n    return sum(values)\n"
        self.harmful = b"def total(values):\n    return sum(values) if values else 1\n"
        self.manifest = {"source_prefix": "pkg/", "source_files_sha256": {"pkg/__init__.py": sha256(b""), "pkg/calc.py": sha256(self.original)}}
        for folder in (self.baseline, self.work):
            (folder / "pkg").mkdir(parents=True)
            (folder / "pkg/__init__.py").write_bytes(b"")
            (folder / "pkg/calc.py").write_bytes(self.original)
        (self.work / "temp").mkdir()
        (self.work / "TASK.md").write_bytes(self.issue)
        self.run_id = "fixture-" + uuid.uuid4().hex
        self.before = self.seal(self.v1, "v1")
        self.reviewer = RoleIdentity(run_id=self.run_id, role_run_id="reviewer", session_id="review-session", role="reviewer")
        c = self.before.candidate
        handoff = Handoff(handoff_id="handoff", run_id=self.run_id, from_role_run_id=c.producer_role_run_id,
            to_role_run_id=self.reviewer.role_run_id, task_digest=c.task_digest, baseline_digest=c.baseline_digest,
            candidate_id=c.candidate_id, source_tree_digest=c.source_tree_digest, patch_sha256=c.patch_sha256,
            snapshot_id=c.snapshot_id, read_document_ids=tuple(d.document_id for d in c.documents),
            questions=("Review these synthetic declared cases only.",))
        self.broker = ReviewEvidenceBroker(self.before, self.reviewer, handoff)
        self.ev = self.broker.dispatch(self.reviewer, "read_diff", {"candidate_id": c.candidate_id})["evidence"]["evidence_id"]
        findings = []
        for name, claim in (("negative", "The declared negative input loses its negative sum."),
                            ("duplicate-negative", "Duplicate description of the same negative-input defect."),
                            ("positive", "The declared positive input does not return its sum."),
                            ("unknown", "An unregistered behavior might be wrong.")):
            findings.append({"finding_id": name, "severity": "major", "category": "behavior", "claim": claim,
                "requirement_ref": "issue:paragraph-1", "impact": "Offline artificial claim, not model discovery.", "evidence_ids": [self.ev]})
        self.review = ReviewReport.parse({"review_id": "review", "run_id": self.run_id, "reviewer_role_run_id": self.reviewer.role_run_id,
            "candidate_id": c.candidate_id, "source_tree_digest": c.source_tree_digest, "verdict": "request_changes",
            "coverage": ["Artificial offline claims over three declared cases"], "findings": findings})
        self.probe = self.home / "registered_checks.py"
        self.probe.write_text(PROBE, encoding="utf-8")
        self.plan = ProbePlan.parse({"plan_id": "sum-behavior", "task_digest": c.task_digest, "probe_sha256": sha256(self.probe.read_bytes()),
            "scope": "Three declared arithmetic inputs; not a general proof or real-model score.", "checks": [
                {"check_id": name, "test_id": "registered_probe.Behavior.test_" + name, "requirement_ref": "issue:paragraph-1", "prior_visibility": visibility}
                for name in ("empty", "negative", "positive")]})
        self.judge = OfflineAdjudicator(self.home / "adjudication")
        self.key = self.judge.register_probe(self.plan, self.probe)
        self.bindings = tuple(FindingBinding(review_id=self.review.review_id, finding_id=f.finding_id,
            finding_sha256=sha256(canonical_bytes(f)), claim_key="negative-sum" if "negative" in f.finding_id else "positive-sum",
            check_ids=("negative" if "negative" in f.finding_id else "positive",),
            coverage_scope="Exact declared negative input [-3,-2]" if "negative" in f.finding_id else "Exact declared positive input [2,3]")
            for f in self.review.findings if f.finding_id != "unknown")

    def seal(self, raw, name):
        (self.work / "pkg/calc.py").write_bytes(raw)
        return seal_candidate(self.manifest, self.baseline, self.work, self.issue, self.home / name,
            run_id=self.run_id, producer_role_run_id="repairer")

    def decide(self, target, *, before_receipt=None, after_receipt=None, bindings=None):
        return self.judge.adjudicate(self.review, self.reviewer, self.before, target,
            evidence=self.broker.evidence, reports={}, plan_key=self.key,
            reviewed_receipt_id=before_receipt.receipt_id if before_receipt else None,
            target_receipt_id=after_receipt.receipt_id if after_receipt else None,
            bindings=self.bindings if bindings is None else bindings)


def main():
    f = AdjudicationFixture()
    before = f.judge.run_probe(f.before, f.key)
    examples = {}
    for name, raw in (("improved", f.good), ("harmful", f.harmful), ("no-demonstrated-benefit", f.v1 + b"# unrelated change\n")):
        target = f.seal(raw, name)
        after = f.judge.run_probe(target, f.key)
        decisions = f.decide(target, before_receipt=before, after_receipt=after)
        examples[name] = {"decisions": [d.model_dump(mode="json") for d in decisions], "summary": decision_summary(decisions)}
    result = {"scope": "All claims, mappings and changes are artificial. Checks really ran in separate processes; no models, production DB or budget changes.",
              "run_id": f.run_id, "examples": examples}
    (f.home / "demonstration.json").write_bytes(canonical_bytes(result))
    print(json.dumps({"output": str(f.home / "demonstration.json"), "scope": result["scope"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
