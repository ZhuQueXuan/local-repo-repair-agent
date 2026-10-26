"""Meaningful offline checks for scope, version binding and harmful revisions."""
import json
from pathlib import Path
from unittest.mock import patch
import unittest

from adjudication_demo import AdjudicationFixture
from review_adjudication import OfflineAdjudicator, FindingBinding, decision_summary
from collaboration_protocol import ProtocolError, ReviewReport, canonical_bytes, sha256


class AdjudicationTests(unittest.TestCase):
    def setUp(self):
        self.f = AdjudicationFixture()

    def evaluate(self, raw, name="target"):
        f = self.f
        target = f.seal(raw, name)
        before = f.judge.run_probe(f.before, f.key)
        after = f.judge.run_probe(target, f.key)
        return target, before, after, f.decide(target, before_receipt=before, after_receipt=after)

    def test_real_process_confirms_refutes_leaves_unknown_and_deduplicates(self):
        f = self.f
        target, before, after, decisions = self.evaluate(f.good)
        self.assertEqual([d.verdict for d in decisions], ["confirmed", "confirmed", "refuted", "unresolved"])
        self.assertEqual(decisions[0].resolution, "resolved_on_checked_cases")
        self.assertEqual(decisions[0].impact, "improved_on_checked_cases")
        self.assertEqual(decisions[0].improved_checks, ("negative",))
        summary = decision_summary(decisions)
        self.assertEqual(summary["duplicate_findings"], 1)
        self.assertEqual(summary["distinct_confirmed_groups"], 1)
        self.assertEqual(summary["new_confirmed_groups_within_scope"], 1)
        self.assertEqual(summary["unresolved"], 1)
        self.assertEqual(len(f.review.findings), 4)
        self.assertNotEqual(before.candidate_id, after.candidate_id)
        old = next(d for d in f.before.candidate.documents if d.side == "candidate" and d.path == "pkg/calc.py")
        self.assertIn(b"max(0", f.before.read(old.document_id))

    def test_fixing_a_finding_does_not_hide_a_new_regression(self):
        _, _, _, decisions = self.evaluate(self.f.harmful)
        d = decisions[0]
        self.assertEqual(d.resolution, "resolved_on_checked_cases")
        self.assertEqual(d.impact, "harmful_on_checked_cases")
        self.assertEqual(d.improved_checks, ("negative",))
        self.assertEqual(d.regressed_checks, ("empty",))

    def test_changed_source_without_behavioral_gain_is_not_improvement(self):
        _, _, _, decisions = self.evaluate(self.f.v1 + b"# unrelated comment\n")
        self.assertEqual(decisions[0].resolution, "not_resolved")
        self.assertEqual(decisions[0].impact, "changed_without_checked_improvement")

    def test_no_mapping_or_receipt_never_confirms_on_model_confidence(self):
        decisions = self.f.decide(self.f.before, bindings=())
        self.assertTrue(all(d.verdict == "unresolved" for d in decisions))
        self.assertTrue(all(d.resolution == "unresolved" for d in decisions))
        self.assertEqual(decision_summary(decisions)["distinct_confirmed_groups"], 0)

    def test_same_candidate_is_unchanged_and_probe_runs_only_once(self):
        f = self.f
        first = f.judge.run_probe(f.before, f.key)
        with patch("review_adjudication.subprocess.Popen") as launch:
            again = f.judge.run_probe(f.before, f.key)
        launch.assert_not_called()
        self.assertEqual(first, again)
        d = f.decide(f.before, before_receipt=first, after_receipt=first)[0]
        self.assertEqual(d.impact, "unchanged")
        self.assertEqual(d.resolution, "not_resolved")

    def test_old_receipt_cannot_be_applied_to_target_candidate(self):
        f = self.f
        target = f.seal(f.good, "fixed")
        old = f.judge.run_probe(f.before, f.key)
        with self.assertRaisesRegex(ProtocolError, "receipt_version_mismatch"):
            f.decide(target, before_receipt=old, after_receipt=old)

    def test_cross_run_lineage_is_rejected_even_for_identical_code(self):
        other = AdjudicationFixture()
        with self.assertRaisesRegex(ProtocolError, "lineage_mismatch"):
            self.f.decide(other.before)

    def test_model_or_json_receipt_is_not_program_issued(self):
        with self.assertRaisesRegex(ProtocolError, "not_program_issued"):
            self.f.judge.adjudicate(self.f.review, self.f.reviewer, self.f.before, self.f.before,
                evidence=self.f.broker.evidence, reports={}, plan_key=self.f.key,
                reviewed_receipt_id="invented-by-model", bindings=self.f.bindings)

    def test_tampered_receipt_or_decision_is_rejected(self):
        f = self.f
        old = f.judge.run_probe(f.before, f.key)
        output = f.judge._receipts[old.receipt_id][1]
        (output / "receipt-results.json").write_text('{"results":{"negative":"passed"}}', encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "receipt_file_changed"):
            f.decide(f.before, before_receipt=old, after_receipt=old)

    def test_decisions_are_append_only_and_same_claim_group_has_same_scope(self):
        f = self.f
        _, before, after, decisions = self.evaluate(f.good)
        target = f.judge._receipts[after.receipt_id][2]
        again = f.decide(target, before_receipt=before, after_receipt=after)
        self.assertEqual(decisions, again)
        bad = FindingBinding.parse(f.bindings[1].model_dump(mode="json") | {"check_ids": ["positive"]})
        with self.assertRaisesRegex(ProtocolError, "different_checked_scope"):
            f.decide(target, before_receipt=before, after_receipt=after, bindings=(f.bindings[0], bad))
        path = f.judge.home / (decisions[0].decision_id + ".json")
        path.write_text("changed decision", encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "adjudication_record_changed"):
            f.decide(target, before_receipt=before, after_receipt=after)

    def test_probe_mutation_does_not_get_a_passing_receipt_for_original_bytes(self):
        f = self.f
        raw = b"from pathlib import Path\nPath(__file__).write_text('def total(values): return sum(values)\\n')\ndef total(values): return sum(values)\n"
        target = f.seal(raw, "self-mutation")
        receipt = f.judge.run_probe(target, f.key)
        self.assertEqual(receipt.status, "unavailable")
        results = f.judge._receipts[receipt.receipt_id][3]
        self.assertEqual(set(results.values()), {"unknown"})
        target.verify()

    def test_child_does_not_inherit_parent_api_credentials(self):
        f = self.f
        raw = b"import os\ndef total(values):\n    assert 'DEEPSEEK_API_KEY' not in os.environ and 'BUPT_API_KEY' not in os.environ\n    return sum(values)\n"
        target = f.seal(raw, "no-parent-secrets")
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "offline-only-secret", "BUPT_API_KEY": "offline-only-other"}):
            receipt = f.judge.run_probe(target, f.key)
        self.assertEqual(set(f.judge._receipts[receipt.receipt_id][3].values()), {"passed"})

    def test_parent_cancellation_terminates_owned_probe_without_receipt(self):
        f = self.f
        target = f.seal(b"import time\ntime.sleep(60)\ndef total(values): return sum(values)\n", "cancelled")
        import subprocess
        launch, processes = subprocess.Popen, []
        def record(*args, **kwargs):
            process = launch(*args, **kwargs)
            processes.append(process)
            return process
        calls = []
        def check():
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError("offline parent cancellation")
        with patch("review_adjudication.subprocess.Popen", side_effect=record), self.assertRaisesRegex(RuntimeError, "parent cancellation"):
            f.judge.run_probe(target, f.key, check=check)
        self.assertTrue(processes)
        self.assertTrue(all(p.poll() is not None for p in processes))
        self.assertFalse(f.judge._receipts)

    def test_frozen_probe_is_not_polluted_by_later_original_edits(self):
        f = self.f
        f.probe.write_text("raise RuntimeError('later original change')", encoding="utf-8")
        receipt = f.judge.run_probe(f.before, f.key)
        self.assertEqual(receipt.status, "completed")
        self.assertEqual(f.judge._receipts[receipt.receipt_id][3]["negative"], "failed")
        directory = f.judge._plans[f.key][1]
        (directory / "probe.py").write_text("tampered frozen copy", encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "frozen_probe_changed"):
            f.judge.run_probe(f.before, f.key)

    def test_altered_claim_or_unknown_check_cannot_reuse_a_binding(self):
        f = self.f
        b = f.bindings[0]
        changed = FindingBinding.parse(b.model_dump(mode="json") | {"finding_sha256": "0" * 64})
        with self.assertRaisesRegex(ProtocolError, "finding_mapping_mismatch"):
            f.decide(f.before, bindings=(changed,))
        unknown = FindingBinding.parse(b.model_dump(mode="json") | {"check_ids": ["not-registered"]})
        with self.assertRaisesRegex(ProtocolError, "requirement_mismatch"):
            f.decide(f.before, bindings=(unknown,))

    def test_already_reported_failure_is_not_a_new_finding(self):
        f = AdjudicationFixture(visibility="already_reported")
        target = f.seal(f.good, "fixed")
        before, after = f.judge.run_probe(f.before, f.key), f.judge.run_probe(target, f.key)
        summary = decision_summary(f.decide(target, before_receipt=before, after_receipt=after))
        self.assertEqual(summary["new_confirmed_groups_within_scope"], 0)
        self.assertEqual(summary["already_reported_confirmed_groups"], 1)

    def test_check_errors_and_timeouts_remain_unresolved(self):
        f = self.f
        (f.work / "pkg/calc.py").write_text("raise RuntimeError('cannot import')\n", encoding="utf-8")
        target = f.seal((f.work / "pkg/calc.py").read_bytes(), "broken-import")
        old = f.judge.run_probe(f.before, f.key)
        receipt = f.judge.run_probe(target, f.key)
        decisions = f.decide(target, before_receipt=old, after_receipt=receipt)
        self.assertEqual(decisions[0].resolution, "unresolved")
        self.assertEqual(decisions[0].impact, "unresolved")
        slow = f.seal(b"import time\ntime.sleep(60)\ndef total(values): return sum(values)\n", "slow")
        timeout = f.judge.run_probe(slow, f.key, timeout=.2)
        self.assertEqual(timeout.status, "unavailable")
        self.assertEqual(f.decide(slow, before_receipt=old, after_receipt=timeout)[0].resolution, "unresolved")

    def test_different_revision_summaries_are_not_silently_combined(self):
        _, _, _, improved = self.evaluate(self.f.good, "fixed")
        _, _, _, harmful = self.evaluate(self.f.harmful, "harmful")
        with self.assertRaisesRegex(ProtocolError, "mixes_evaluation_contexts"):
            decision_summary(improved + harmful)

    def test_suggested_model_code_is_not_executed(self):
        f = self.f
        data = f.review.model_dump(mode="json")
        data["findings"][0]["suggested_validation"] = {"input": "raise RuntimeError('model code must not run')", "expected": "invented"}
        f.review = ReviewReport.parse(data)
        receipt = f.judge.run_probe(f.before, f.key)
        decisions = f.decide(f.before, before_receipt=receipt, after_receipt=receipt, bindings=())
        self.assertTrue(all(d.verdict == "unresolved" for d in decisions))

    def test_store_requires_temp_and_probe_hash_is_checked(self):
        from review_tools import ROOT
        with self.assertRaises(ValueError):
            OfflineAdjudicator(ROOT / "data/not-authorized-adjudication")
        self.f.probe.write_text("different code", encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "probe_source_hash_mismatch"):
            self.f.judge.register_probe(self.f.plan, self.f.probe)


if __name__ == "__main__":
    unittest.main(verbosity=2)
