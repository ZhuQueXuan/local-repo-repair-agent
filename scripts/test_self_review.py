"""S+ contracts and real local execution with synthetic model transport."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from collaboration import RoleContext
from collaboration_panel import collaboration_view, create_panel_run, public_policy
from collaboration_protocol import ProtocolError, canonical_bytes, sha256
from collaboration_runtime import ProtectedSelfReviewModel, RepairSession
from collaboration_store import CollaborationStore, StateConflict
from context_budget import ContextBudget, message_bytes
from minisweagent.exceptions import LimitsExceeded
from self_review import SelfReviewSession, validate_self_policy
from self_review_demo import exercise
from task_service_store import ROOT
import test_collaboration_runtime as runtime


class SelfReviewIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store, cls.runner, cls.summary, cls.wires = exercise(attack_bash=True)

    def test_complete_four_phase_chain_with_real_checks_and_replay(self):
        row = self.summary["run"]
        self.assertEqual((row["status"], row["repair_result"], row["review_verdict"]), ("finished", "passed", "approve"), row["reason"])
        self.assertEqual([r["status"] for r in self.store.roles(row["id"])], ["succeeded"] * 4)
        self.assertEqual(self.summary["counts"], {"repairing": 2, "reviewing": 3, "revising": 2, "final_review": 2})
        report = json.loads((self.runner.output / "patch-replay/report.json").read_bytes())
        self.assertTrue(report["task_tests_passed"])
        self.assertEqual(len(self.store.records(row["id"], "response")), 1)

    def test_one_logical_session_with_distinct_capability_and_fee_ids(self):
        roles = self.store.roles(self.summary["run_id"])
        self.assertEqual(len({r["session_id"] for r in roles}), 1)
        self.assertEqual(len({r["role_run_id"] for r in roles}), 4)
        self.assertEqual(len(self.summary["usage"]["by_role"]), 4)
        self.assertEqual(self.summary["usage"]["attempts"], 9)
        self.assertEqual(self.summary["usage"]["logical_requests"], 9)
        self.assertFalse(self.summary["usage"]["has_unknown_usage"])
        self.assertEqual(len(set(self.summary["usage"]["tickets"])), 9)

    def test_read_only_phases_cannot_execute_the_returned_bash_calls(self):
        expected = {"list_files", "read_lines", "search_text", "read_diff", "read_report", "submit_review"}
        for wire in self.wires:
            with self.subTest(phase=wire["phase"]):
                self.assertEqual(set(wire["tool_names"]), {"bash"} if wire["phase"] in {"repairing", "revising"} else expected)
        commands = [e["data"]["command"] for e in self.store.events(self.summary["run_id"]) if e["kind"] == "action_started"]
        self.assertFalse(any("SHOULD_NEVER_EXECUTE" in c for c in commands))
        self.assertEqual(sum(c == "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" for c in commands), 2)

    def test_author_and_self_check_history_survives_all_transitions(self):
        marker = "OFFLINE_SAME_AUTHOR_" + self.summary["run_id"]
        for wire in self.wires:
            if wire["phase"] != "repairing":
                self.assertIn(marker, canonical_bytes(wire["messages"]).decode())
            if wire["phase"] in {"reviewing", "final_review"}:
                self.assertIn("SAME repair Agent", wire["messages"][0]["content"])
                self.assertNotIn("You independently review", wire["messages"][0]["content"])
            if wire["phase"] in {"revising", "final_review"}:
                self.assertIn('"name":"submit_review"', canonical_bytes(wire["messages"]).decode())
            self.assertLessEqual(message_bytes(wire["messages"]), 16384)
        final = json.loads((self.runner.output / "final_review-trajectory.json").read_bytes())
        self.assertIn("OFFLINE_REVISION_HISTORY", canonical_bytes(final).decode())
        self.assertEqual(sum(bool(m.get("extra", {}).get("completed_submission_receipt")) for m in final["messages"]), 2)
        self.assertEqual([e["phase"] for e in self.summary["history_events"]], ["reviewing", "revising", "final_review"])

    def test_historical_tool_calls_all_have_real_matching_receipts(self):
        for wire in self.wires:
            outstanding = set()
            for message in wire["messages"]:
                if message["role"] == "assistant":
                    outstanding.update(c["id"] for c in message.get("tool_calls", []))
                if message["role"] == "tool":
                    self.assertIn(message["tool_call_id"], outstanding)
                    outstanding.remove(message["tool_call_id"])
            self.assertFalse(outstanding)

    def test_panel_identifies_synthetic_self_check_and_uses_its_temp_ledger(self):
        value = collaboration_view(self.store, self.summary["run_id"])
        self.assertEqual(value["mode"], "self_review_live")
        self.assertTrue(value["synthetic"])
        self.assertTrue(value["usage"]["synthetic"])
        self.assertEqual(value["usage"]["attempts"], 9)
        self.assertEqual(value["adjudications"], [])

    def test_approve_and_insufficient_branches_do_not_force_another_edit(self):
        for scenario, verdict in (("approve", "approve"), ("insufficient", "insufficient_evidence")):
            with self.subTest(scenario=scenario):
                store, runner, summary, _ = exercise(scenario=scenario)
                self.assertEqual(summary["run"]["status"], "finished")
                self.assertEqual(summary["run"]["review_verdict"], verdict)
                self.assertEqual([r["status"] for r in store.roles(summary["run_id"])], ["succeeded", "succeeded", "skipped", "skipped"])
                self.assertEqual(set(summary["counts"]), {"repairing", "reviewing"})

    def test_provider_failure_keeps_candidate_and_unknown_cost(self):
        store, runner, summary, _ = exercise(scenario="provider_failure")
        self.assertEqual(summary["run"]["status"], "failed")
        self.assertEqual(summary["run"]["repair_result"], "passed")
        self.assertEqual(summary["run"]["collaboration_result"], "degraded")
        self.assertEqual(summary["usage"]["attempts"], 3)
        self.assertIsNone(summary["usage"]["complete_cost_estimate_cny"])
        self.assertEqual(summary["usage"]["unknown_cost_attempts"], 1)

    def test_cancel_at_self_check_starts_no_self_check_request(self):
        store, runner, summary, _ = exercise(cancel_phase="reviewing")
        self.assertEqual(summary["run"]["status"], "cancelled")
        self.assertEqual(summary["counts"], {"repairing": 2})
        self.assertEqual(summary["usage"]["attempts"], 2)
        self.assertFalse(store.records(summary["run_id"], "review"))

    def test_submission_and_completed_review_tampering_cannot_advance(self):
        for fault in ("submission", "review_history"):
            def tamper(name, role, runner):
                if name != "after_role_claim": return
                if fault == "submission" and role["phase"] == "reviewing":
                    runner.session.agent.env.completed_submission = None
                if fault == "review_history" and role["phase"] == "revising":
                    runner.self_session.completed["history"][1]["content"] += "changed later"
            with self.subTest(fault=fault):
                store, runner, summary, _ = exercise(checkpoint=tamper)
                self.assertEqual(summary["run"]["status"], "failed")
                self.assertEqual(summary["run"]["repair_result"], "passed")
                self.assertNotIn("revising", summary["counts"])
                self.assertFalse(store.records(summary["run_id"], "response"))


class SelfReviewContractTests(unittest.TestCase):
    def setUp(self):
        runtime.RuntimeTests.setUp(self)

    def model(self):
        return runtime.RuntimeTests.model(self, cls=ProtectedSelfReviewModel,
            context_budget=ContextBudget(target_bytes=12288, keep_recent_tool_outputs=1))

    def test_policy_keeps_phase_limits_and_rejects_ambiguous_modes(self):
        policy = public_policy(self_review=True)
        dual = public_policy()
        self.assertEqual(policy["role_seconds"], dual["role_seconds"])
        self.assertEqual(policy["parent_seconds"], dual["parent_seconds"])
        self.assertNotIn("context_mode", dual)
        for bad in (policy | {"context_mode": "unknown"}, policy | {"diagnostic_profile": "seeded_falsey_remainder_v1"}, policy | {"feedback_windows": 2}):
            with self.subTest(bad=bad), self.assertRaises(ValueError): validate_self_policy(bad, "adaptive_history")
        with self.assertRaises(ValueError): validate_self_policy(policy, "raw")
        with self.assertRaises(ValueError): public_policy(True, self_review=True)

    def test_panel_creation_idempotency_and_existing_dual_sessions_unchanged(self):
        body = {"task": "split-after-boundary", "profile": "adaptive_history", "mode": "live", "collaboration_mode": "self_review", "policy_version": 1, "request_key": uuid.uuid4().hex}
        row = create_panel_run(self.store, body)
        self.assertEqual(row, create_panel_run(self.store, body))
        self.assertEqual(len({r["session_id"] for r in self.store.roles(row["id"])}), 1)
        with self.assertRaises(StateConflict): create_panel_run(self.store, body | {"collaboration_mode": "dual_review"})
        dual = create_panel_run(self.store, body | {"request_key": uuid.uuid4().hex, "collaboration_mode": "dual_review"})
        self.assertEqual(len({r["session_id"] for r in self.store.roles(dual["id"])}), 3)
        for extra in ({"mode": "replay"}, {"profile": "raw"}, {"transport_scope": "scripted_offline"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError): create_panel_run(self.store, body | extra)

    def test_interrupted_identity_is_not_automatically_resumed(self):
        row = create_panel_run(self.store, {"task":"split-after-boundary", "profile":"adaptive_history", "mode":"live", "collaboration_mode":"self_review", "policy_version":1,"request_key":uuid.uuid4().hex})
        self.store.claim_parent(row["id"])
        self.store.claim_role(row["id"], "repairing", "f"*64, self.store.state(row["id"])["collaboration_state"]["revision"])
        self.store.reconcile_collaborations()
        from collaboration_live import run_live_worker
        with patch("collaboration_live.LiveRunner") as launch:
            self.assertEqual(run_live_worker(self.store, row["id"])["status"], "interrupted")
        launch.assert_not_called()

    def test_wrong_session_or_missing_completed_history_is_rejected(self):
        session = SelfReviewSession(SimpleNamespace(context=self.context))
        role = self.context.role | {"phase": "reviewing", "session_id": "other-session"}
        with self.assertRaisesRegex(ProtocolError, "identity_mismatch"):
            session.begin(RoleContext(self.store, role, self.context.deadline))
        role = self.context.role | {"phase": "revising"}
        with self.assertRaisesRegex(ProtocolError, "history_missing"):
            session.resume(RoleContext(self.store, role, self.context.deadline), "feedback")

    def test_no_fabricated_submit_receipt_from_exit_text(self):
        agent = SimpleNamespace(messages=[{"role":"assistant"},{"role":"exit","extra":{"exit_status":"Submitted"}}], env=SimpleNamespace(completed_submission=None))
        with self.assertRaisesRegex(ProtocolError, "cannot_prove"):
            RepairSession(agent, self.context).submitted_history()

    def test_projection_keeps_current_phase_when_format_correction_follows_it(self):
        model = self.model()
        metadata = {"run_id":"run", "review_id":"old", "candidate_id":"c", "source_tree_digest":"f"*64,"snapshot_id":"s","report_ids":[]}
        old = "Use registered IDs below.\n" + json.dumps(metadata | {"documents": "x"*15000})
        current = "Use registered IDs below.\n" + json.dumps(metadata | {"review_id":"current"})
        messages = [{"role":"system","content":"read only"},{"role":"user","content":"ORIGINAL ISSUE"},
            {"role":"assistant","content":"AUTHOR REASONING TO RETAIN"},{"role":"user","content":old},
            {"role":"user","content":current},{"role":"user","content":"Invalid review tool call: use read-only tools"}]
        before = canonical_bytes(messages)
        wire = model._prepare_messages_for_api(messages)
        self.assertIn(current, [m.get("content") for m in wire])
        self.assertIn("AUTHOR REASONING TO RETAIN", [m.get("content") for m in wire])
        self.assertIn(sha256(old.encode()), canonical_bytes(wire).decode())
        self.assertLess(message_bytes(wire),16384)
        self.assertEqual(canonical_bytes(messages),before)

    def test_non_json_historical_bash_output_projects_without_json_parsing(self):
        model = self.model()
        messages=[{"role":"system","content":"read only"},{"role":"user","content":"original issue"},
            {"role":"assistant","content":None,"tool_calls":[{"id":"old-bash","type":"function","function":{"name":"bash","arguments":"{}"}}]},
            {"role":"tool","tool_call_id":"old-bash","content":"not json "+"x"*18000,"extra":{"returncode":0,"tool_output":{"full_output_path":"temp/tool-output/old.txt","raw_bytes":18009,"sequence":1}}},
            {"role":"assistant","content":None,"tool_calls":[{"id":"read","type":"function","function":{"name":"read_diff","arguments":"{}"}}]},
            {"role":"tool","tool_call_id":"read","content":"{}"}]
        wire=model._prepare_messages_for_api(messages)
        self.assertLess(message_bytes(wire),16384)
        self.assertEqual([m["tool_call_id"] for m in wire if m["role"]=="tool"],["old-bash","read"])

    def test_irreducible_context_hits_original_guard_before_reserving_cost(self):
        model=self.model()
        with patch("litellm.completion") as transport, self.assertRaises(LimitsExceeded):
            model.query([{"role":"user","content":"x"*20000}])
        transport.assert_not_called()
        self.assertEqual(set(json.loads(self.path.read_bytes())["requests"]),{"old"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
