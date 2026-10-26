"""Replay the actual malformed replies without silently rewriting evidence."""
import json
import unittest

from collaboration_responses import parse_response_drafts, InvalidFindingResponses, response_correction
from collaboration_protocol import canonical_bytes, ReviewReport, EvidenceRef
from test_collaboration_protocol import StageAFixture
from review_tools import ROOT


class ResponseTests(unittest.TestCase):
    def setUp(self):
        self.f = StageAFixture()
        self.review = self.f.review()
        self.row = {"finding_id": "finding-1", "disposition": "accepted", "reason": "Original defect acknowledged, independent checks still required", "evidence_ids": []}

    def parse(self, rows):
        return parse_response_drafts(canonical_bytes(rows), self.review, self.f.broker.evidence, self.f.manifest, self.f.work)

    def test_actual_failed_reply_paths_are_rejected_and_file_untouched(self):
        archive = ROOT / "experiments/multi_agent_feedback_recheck_live_20261024"
        path = archive / "invalid-finding-responses.json"
        raw = path.read_bytes()
        review = ReviewReport.parse(json.loads((archive / "run-readable/reviews.json").read_text(encoding="utf-8"))[0])
        evidence = {v["evidence_id"]: EvidenceRef.parse(v) for v in json.loads((archive / "run-readable/evidences.json").read_text(encoding="utf-8"))}
        with self.assertRaisesRegex(InvalidFindingResponses, "invalid_response_fields_or_evidence_ids"):
            parse_response_drafts(raw, review, evidence, self.f.manifest, self.f.work)
        self.assertEqual(raw, path.read_bytes())
        rows = json.loads(raw)
        for row in rows:
            row["evidence_ids"] = []
        drafts = parse_response_drafts(canonical_bytes(rows), review, evidence, self.f.manifest, self.f.work)
        self.assertEqual(len(drafts), 2)
        self.assertTrue(all(v.disposition == "accepted" and not v.evidence_ids for v in drafts))
        self.assertNotIn("resolved", drafts[0].model_dump())

    def test_accepted_and_needs_evidence_can_use_empty_lists(self):
        self.assertEqual(self.parse([self.row])[0].disposition, "accepted")
        self.assertEqual(self.parse([self.row | {"disposition": "needs_evidence"}])[0].disposition, "needs_evidence")
        self.assertEqual(self.parse([]), ())

    def test_rejected_requires_existing_evidence_for_unchanged_target(self):
        with self.assertRaisesRegex(InvalidFindingResponses, "requires_current_registered_evidence"):
            self.parse([self.row | {"disposition": "rejected"}])
        ev = self.review.findings[0].evidence_ids[0]
        row = self.row | {"disposition": "rejected", "evidence_ids": [ev]}
        self.assertEqual(self.parse([row])[0].evidence_ids, (ev,))
        self.f.write(self.f.v2)
        with self.assertRaisesRegex(InvalidFindingResponses, "not_bound_to_current_source"):
            self.parse([row])

    def test_unknown_or_other_finding_is_not_dropped(self):
        for changes, error in (({"evidence_ids": ["invented-id"]}, "not_registered"),
                               ({"finding_id": "other-finding"}, "finding_not_in_current_review")):
            with self.subTest(changes=changes), self.assertRaisesRegex(InvalidFindingResponses, error):
                self.parse([self.row | changes])
        with self.assertRaisesRegex(InvalidFindingResponses, "duplicate_finding_response"):
            self.parse([self.row, self.row])

    def test_size_shape_and_strict_types_are_bounded(self):
        for value in ({}, [self.row] * 21, [self.row | {"unexpected": True}], [self.row | {"evidence_ids": "ev-1"}],
                      [self.row | {"reason": "x" * 2001}], [self.row | {"disposition": "fixed"}]):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(InvalidFindingResponses):
                self.parse(value)
        with self.assertRaisesRegex(InvalidFindingResponses, "file_too_large"):
            parse_response_drafts(b" " * 16385, self.review, {}, self.f.manifest, self.f.work)
        with self.assertRaisesRegex(InvalidFindingResponses, "not_json"):
            parse_response_drafts(b"not json", self.review, {}, self.f.manifest, self.f.work)

    def test_correction_is_explicit_and_does_not_echo_untrusted_file_values(self):
        text = response_correction("invalid_response_fields_or_evidence_ids", self.review)
        self.assertIn("NEVER put file paths", text)
        self.assertIn("[] is allowed", text)
        self.assertIn("original deadline", text)
        self.assertIn("finding-1", text)
        self.assertNotIn("temp/tool-output/0014.txt", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
