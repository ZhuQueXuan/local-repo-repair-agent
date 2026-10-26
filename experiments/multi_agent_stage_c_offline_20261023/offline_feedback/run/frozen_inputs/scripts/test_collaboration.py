"""State-machine branches, actual SQLite persistence and crash checkpoints."""
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest

from collaboration import CollaborationCoordinator, attributed_usage
from collaboration_mock import ScriptedRunner
from collaboration_protocol import ProtocolError, canonical_bytes, sha256
from collaboration_store import CollaborationStore, StateConflict
from deepseek_budget import SharedBudget
from task_service_store import ROOT, worker_lock
from test_collaboration_store import make_store


class InjectedCrash(BaseException):
    pass


class CoordinatorTests(unittest.TestCase):
    def test_feedback_path_persists_all_outputs_and_unique_role_costs(self):
        store, parent = make_store()
        runner = ScriptedRunner(store, accounting=True)
        result = CollaborationCoordinator(store, runner).run(parent["id"])
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["repair_result"], "unknown")
        self.assertEqual(runner.calls, ["repairing", "reviewing", "revising", "final_review"])
        reopened = CollaborationStore(store.home)
        self.assertEqual(len(reopened.records(parent["id"], "handoff")), 2)
        self.assertEqual(len(reopened.records(parent["id"], "review")), 2)
        self.assertEqual(len(reopened.records(parent["id"], "response")), 1)
        budget = SharedBudget(runner.config, ledger_path=runner.ledger_path, temp_dir=store.home / "accounting-temp")
        usage = attributed_usage(budget, parent["id"])
        self.assertEqual(usage["attempts"], 4)
        self.assertEqual(len(usage["by_role"]), 4)
        self.assertEqual(sum(r["attempts"] for r in usage["by_role"].values()), usage["attempts"])
        self.assertEqual(usage["known_price_estimate_cny"], 0.004)

    def test_approve_branch_skips_revision_and_final_review(self):
        store, parent = make_store()
        runner = ScriptedRunner(store, verdict="approve")
        result = CollaborationCoordinator(store, runner).run(parent["id"])
        self.assertEqual(runner.calls, ["repairing", "reviewing"])
        self.assertEqual(result["review_verdict"], "approve")
        self.assertEqual([r["status"] for r in store.roles(parent["id"])], ["succeeded", "succeeded", "skipped", "skipped"])
        self.assertEqual(result["repair_result"], "unknown")

    def test_insufficient_evidence_ends_without_silent_approval(self):
        store, parent = make_store()
        runner = ScriptedRunner(store, verdict="insufficient_evidence")
        result = CollaborationCoordinator(store, runner).run(parent["id"])
        self.assertEqual(result["review_verdict"], "insufficient_evidence")
        self.assertEqual(len(runner.calls), 2)

    def test_unanswered_feedback_is_not_converted_to_final_approval(self):
        store, parent = make_store()
        runner = ScriptedRunner(store)
        def omit_responses(context):
            result = runner(context)
            if context.role["phase"] == "revising":
                # Inject an incomplete-but-well-formed runner, not a model call.
                with store.connect() as db:
                    db.execute("DELETE FROM collaboration_objects WHERE run_id=? AND kind='response'", (parent["id"],))
                result["response_ids"] = []
            return result
        result = CollaborationCoordinator(store, omit_responses).run(parent["id"])
        self.assertEqual(result["review_verdict"], "insufficient_evidence")
        self.assertIn("unanswered", result["finding_dispositions"].values())

    def test_cancel_after_role_output_does_not_commit_success_or_start_review(self):
        store, parent = make_store()
        runner = ScriptedRunner(store)
        def checkpoint(name, role):
            if name == "after_role_output":
                store.cancel(parent["id"])
        result = CollaborationCoordinator(store, runner, checkpoint=checkpoint).run(parent["id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(runner.calls, ["repairing"])
        self.assertEqual(store.roles(parent["id"])[0]["status"], "cancelled")

    def test_rejection_and_final_disagreement_do_not_open_second_revision(self):
        store, parent = make_store()
        runner = ScriptedRunner(store, reject=True, final_verdict="request_changes")
        result = CollaborationCoordinator(store, runner).run(parent["id"])
        self.assertEqual(len(runner.calls), 4)
        self.assertEqual(result["review_verdict"], "request_changes")
        response = next(iter(store.records(parent["id"], "response").values()))
        self.assertEqual(response.disposition, "rejected")
        self.assertEqual(response.reviewed_candidate_id, response.target_candidate_id)

    def test_reopening_finished_run_never_calls_runner(self):
        store, parent = make_store()
        runner = ScriptedRunner(store)
        CollaborationCoordinator(store, runner).run(parent["id"])
        before = len(runner.calls)
        CollaborationCoordinator(CollaborationStore(store.home), runner).run(parent["id"])
        self.assertEqual(len(runner.calls), before)

    def test_cancellation_at_every_role_boundary_prevents_next_work(self):
        for phase in ["repairing", "reviewing", "revising", "final_review"]:
            with self.subTest(phase=phase):
                store, parent = make_store()
                runner = ScriptedRunner(store)
                def checkpoint(name, role):
                    if name == "before_role_claim" and role["phase"] == phase:
                        store.cancel(parent["id"])
                result = CollaborationCoordinator(store, runner, checkpoint=checkpoint).run(parent["id"])
                self.assertEqual(result["status"], "cancelled")
                self.assertNotIn(phase, runner.calls)
                self.assertFalse(any(r["status"] in {"running", "pending"} for r in store.roles(parent["id"])))

    def test_crash_at_each_checkpoint_is_observed_without_replay(self):
        for boundary in ["before_role_claim", "after_role_claim", "after_role_output", "after_role_completion", "before_finalization"]:
            with self.subTest(boundary=boundary):
                store, parent = make_store()
                runner = ScriptedRunner(store)
                def checkpoint(name, role):
                    if name == boundary:
                        raise InjectedCrash()
                with self.assertRaises(InjectedCrash):
                    CollaborationCoordinator(store, runner, checkpoint=checkpoint).run(parent["id"])
                before = list(runner.calls)
                result = CollaborationCoordinator(CollaborationStore(store.home), runner).run(parent["id"])
                self.assertEqual(result["status"], "interrupted")
                self.assertEqual(runner.calls, before)
                self.assertTrue(all(r["status"] in {"succeeded", "skipped", "interrupted"} for r in store.roles(parent["id"])))

    def test_actual_child_process_exit_is_reconciled_without_runner(self):
        store, parent = make_store()
        code = """import sys
sys.path.insert(0,sys.argv[1])
from collaboration_store import CollaborationStore
from collaboration_protocol import sha256
s=CollaborationStore(sys.argv[2]);s.claim_parent(sys.argv[3]);s.claim_role(sys.argv[3],'repairing',sha256(b'x'),0)
"""
        subprocess.run([sys.executable, "-B", "-c", code, str(ROOT / "scripts"), str(store.home), parent["id"]], check=True, timeout=15)
        runner = ScriptedRunner(store)
        result = CollaborationCoordinator(CollaborationStore(store.home), runner).run(parent["id"])
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(runner.calls, [])

    def test_role_failure_is_persisted_and_arbitrary_exception_text_not_exposed(self):
        store, parent = make_store()
        def broken(context):
            raise RuntimeError("private exception text must not enter parent")
        result = CollaborationCoordinator(store, broken).run(parent["id"])
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("private exception text", json.dumps(result))
        self.assertEqual(store.roles(parent["id"])[0]["status"], "failed")

    def test_deadline_stops_after_cooperative_callback_without_next_role(self):
        store, parent = make_store({"role_seconds": {"repairing": 0.001, "reviewing": 120, "revising": 180, "final_review": 90}})
        runner = ScriptedRunner(store)
        def slow(context):
            result = runner(context)
            time.sleep(0.02)
            return result
        result = CollaborationCoordinator(store, slow).run(parent["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(runner.calls, ["repairing"])

    def test_single_worker_lock_rejects_second_coordinator(self):
        store, parent = make_store()
        runner = ScriptedRunner(store)
        with worker_lock(store.home, "collaboration.lock") as acquired:
            self.assertTrue(acquired)
            with self.assertRaises(StateConflict):
                CollaborationCoordinator(store, runner).run(parent["id"])
        self.assertEqual(runner.calls, [])

    def test_registered_snapshot_and_records_detect_tampering(self):
        store, parent = make_store()
        runner = ScriptedRunner(store)
        result = CollaborationCoordinator(store, runner).run(parent["id"])
        with store.connect() as db:
            db.execute("UPDATE collaboration_objects SET payload='{}' WHERE kind='review'")
        with self.assertRaises(ProtocolError):
            store.records(parent["id"], "review")
        snapshot = store.snapshot(parent["id"], result["final_candidate_id"])
        doc = next(d for d in snapshot.candidate.documents if d.side == "candidate")
        (snapshot.directory / "objects" / doc.document_id).write_bytes(b"tampered")
        with self.assertRaises(ProtocolError):
            store.snapshot(parent["id"], snapshot.candidate.candidate_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
