"""C integration: protected tool wire, receipts, continuation and deadlines."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parent.parent
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["MSWEA_GLOBAL_CONFIG_DIR"] = str(ROOT / "temp/mini-global")
os.environ["MSWEA_SILENT_STARTUP"] = "1"
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

from litellm import ModelResponse
from collaboration import CollaborationCoordinator, RoleContext, attributed_usage, role_identity
from collaboration_live import LiveRunner
from collaboration_protocol import ProtocolError, ReviewReport, canonical_bytes, sha256
from collaboration_runtime import (LiveRoleBudget, ProtectedReviewModel, RepairModel, RepairSession, run_review)
from collaboration_store import CollaborationStopped, CollaborationStore
from deepseek_budget import SharedBudget, load_budget_config
from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import FormatError, LimitsExceeded, Submitted
from minisweagent.models.test_models import DeterministicToolcallModel, make_toolcall_output
from review_model import REVIEW_TOOLS
from run_deepseek_smoke import BudgetedDeepSeekModel, GitBashEnvironment
from task_service_store import policy
from test_collaboration_protocol import StageAFixture
from test_collaboration_store import make_store, claim_first
import test_provider_priority as priority_tests


def response(tool="read_diff", arguments=None, content=None):
    return ModelResponse(id="offline-response", model="offline-model", choices=[{"index": 0,
        "message": {"role": "assistant", "content": content, "tool_calls": [{"id": "call-" + uuid.uuid4().hex,
            "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments or {})}}]}, "finish_reason": "tool_calls"}],
        usage={"prompt_tokens": 100, "completion_tokens": 20})


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.store, self.parent = make_store()
        self.role = claim_first(self.store, self.parent)
        self.context = RoleContext(self.store, self.role, time.monotonic() + 30)
        self.path = self.store.home / "synthetic-accounting.json"
        self.path.write_bytes(canonical_bytes({"historical_estimated_cost_cny": "7.25", "historical_unknown_charge_reservation_cny": "0.20",
            "blocked": False, "requests": {"old": {"status": "reserved", "amount_cny": "0.10"}}}))
        self.accounting = SharedBudget(load_budget_config(), ledger_path=self.path, temp_dir=self.store.home / "locks")
        self.settings = {
            "BUPT_API_KEY": "primary-test-secret", "BUPT_API_BASE_URL": "https://myai.bupt.edu.cn/llm-gw/v1", "BUPT_CHAT_MODEL": "deepseek-v4-flash",
            "DEEPSEEK_API_KEY": "backup-test-secret", "DEEPSEEK_API_BASE_URL": "https://api.deepseek.com", "DEEPSEEK_CHAT_MODEL": "deepseek-flash"}

    def model(self, cls=ProtectedReviewModel, route=None, **extra):
        return cls(self.settings, context=self.context, accounting=self.accounting, route_state={} if route is None else route,
            ledger_path=self.store.home / (uuid.uuid4().hex + "-ledger.json"), error_path=self.store.home / "error.json", **extra)

    def test_review_wire_has_only_six_tools_and_no_shell(self):
        model = self.model()
        result = response(arguments={"candidate_id": "candidate-1"})
        with patch("review_model.litellm.completion", return_value=result) as transport:
            answer = model.query([{"role": "user", "content": "Review candidate"}])
        kwargs = transport.call_args.kwargs
        self.assertEqual({t["function"]["name"] for t in kwargs["tools"]}, {"list_files", "read_lines", "search_text", "read_diff", "read_report", "submit_review"})
        self.assertNotIn("bash", canonical_bytes(kwargs["tools"]).decode())
        self.assertEqual(answer["extra"]["actions"][0]["tool"], "read_diff")
        usage = attributed_usage(self.accounting, self.parent["id"])
        self.assertEqual(usage["attempts"], 1)
        self.assertEqual(usage["unknown_cost_attempts"], 1)  # Gateway price is unknown.
        self.assertEqual(self.store.request(self.store_event_request())["state"], "received")

    def store_event_request(self):
        return next(e["data"]["logical_request_id"] for e in self.store.events(self.parent["id"]) if e["kind"] == "logical_request_prepared")

    def test_parent_fallback_is_shared_with_new_and_reused_models(self):
        route = {}
        first, reused = self.model(route=route), self.model(route=route)
        reused.request_ordinal = 1  # Distinct logical identity in this synthetic active role.
        with patch("review_model.litellm.completion", side_effect=[priority_tests.PriorityTests.error(503), response(arguments={"candidate_id": "x"}), response(arguments={"candidate_id": "x"})]) as transport:
            first.query([{"role": "user", "content": "one"}])
            reused.query([{"role": "user", "content": "two"}])
        self.assertEqual([c.kwargs["api_key"] for c in transport.call_args_list], ["primary-test-secret", "backup-test-secret", "backup-test-secret"])
        self.assertEqual(route["provider_index"], 1)
        self.assertEqual(self.model(route=route).provider_index, 1)
        usage = attributed_usage(self.accounting, self.parent["id"])
        self.assertEqual(usage["attempts"], 3)
        self.assertEqual(usage["known_prompt_tokens"], 200)
        self.assertTrue(usage["has_unknown_usage"])

    def test_malformed_response_is_accounted_without_a_transport_retry(self):
        model = self.model()
        with patch("review_model.litellm.completion", return_value=response("bash", {"command": "forbidden"})) as transport:
            with self.assertRaises(FormatError) as stopped:
                model.query([{"role": "user", "content": "Review"}])
        self.assertEqual(transport.call_count, 1)
        self.assertIn("response", stopped.exception.messages[0]["extra"])
        self.assertEqual(self.store.request(self.store_event_request())["state"], "received")
        self.assertEqual(attributed_usage(self.accounting, self.parent["id"])["attempts"], 1)

    def test_credentials_in_billed_response_are_not_persisted_as_trajectory(self):
        model = self.model()
        with patch("review_model.litellm.completion", return_value=response(arguments={"candidate_id": "x"}, content="primary-test-secret")):
            with self.assertRaisesRegex(ProtocolError, "credential_in_model_output"):
                model.query([{"role": "user", "content": "Review"}])
        self.assertEqual(attributed_usage(self.accounting, self.parent["id"])["attempts"], 1)
        self.assertNotIn("primary-test-secret", model.ledger_path.read_text())

    def test_cancel_before_request_has_no_ticket(self):
        model = self.model()
        self.store.cancel(self.parent["id"])
        with patch("review_model.litellm.completion") as transport, self.assertRaises(CollaborationStopped):
            model.query([{"role": "user", "content": "Review"}])
        transport.assert_not_called()
        self.assertEqual(attributed_usage(self.accounting, self.parent["id"])["attempts"], 0)

    def test_cancel_after_receipt_preserves_tokens_and_prevents_actions(self):
        model = self.model()
        def cancel_then_return(**kwargs):
            self.store.cancel(self.parent["id"])
            return response(arguments={"candidate_id": "x"})
        with patch("review_model.litellm.completion", side_effect=cancel_then_return) as transport:
            with self.assertRaises(CollaborationStopped):
                model.query([{"role": "user", "content": "Review"}])
        usage = attributed_usage(self.accounting, self.parent["id"])
        self.assertEqual(usage["known_prompt_tokens"], 100)
        self.assertEqual(transport.call_count, 1)

    def test_cancel_on_primary_error_prevents_backup_reservation(self):
        model = self.model()
        def fail(**kwargs):
            self.store.cancel(self.parent["id"])
            raise priority_tests.PriorityTests.error(503)
        with patch("review_model.litellm.completion", side_effect=fail) as transport:
            with self.assertRaises(CollaborationStopped):
                model.query([{"role": "user", "content": "Review"}])
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(attributed_usage(self.accounting, self.parent["id"])["attempts"], 1)

    def test_deadline_reaches_network_timeout_and_prevents_late_request(self):
        self.context.deadline = time.monotonic() + 3
        model = self.model()
        with patch("review_model.litellm.completion", return_value=response(arguments={"candidate_id": "x"})) as transport:
            model.query([{"role": "user", "content": "Review"}])
            self.assertLessEqual(transport.call_args.kwargs["timeout"], 3)
            self.context.deadline = time.monotonic() - 1
            with self.assertRaises(TimeoutError):
                model.query([{"role": "user", "content": "Again"}])
            self.assertEqual(transport.call_count, 1)

    def test_duplicate_logical_identity_never_dispatches_again(self):
        model = self.model()
        with patch("review_model.litellm.completion", return_value=response(arguments={"candidate_id": "x"})) as transport:
            model.query([{"role": "user", "content": "Review"}])
            model.request_ordinal = 0
            with self.assertRaises(ProtocolError):
                model.query([{"role": "user", "content": "Review"}])
            self.assertEqual(transport.call_count, 1)

    def test_reply_projection_keeps_real_tool_ids_without_mutating_raw_history(self):
        model = self.model()
        messages = [{"role": "system", "content": "review"}, {"role": "user", "content": "issue"}]
        for i in range(8):
            call = {"id": "call-" + str(i), "type": "function", "function": {"name": "read_lines", "arguments": "{}"}}
            messages.extend([{"role": "assistant", "content": None, "tool_calls": [call]},
                {"role": "tool", "tool_call_id": call["id"], "content": json.dumps({"content": "x" * 3000, "evidence": {"evidence_id": "ev-" + str(i)}})}])
        before = canonical_bytes(messages)
        projected = model._prepare_messages_for_api(messages)
        self.assertLess(len(canonical_bytes(projected)), len(before))
        self.assertEqual(canonical_bytes(messages), before)
        self.assertEqual([m["tool_call_id"] for m in projected if m["role"] == "tool"], ["call-" + str(i) for i in range(8)])

    def test_actual_reviewer_loop_submits_validated_versioned_report(self):
        fixture = StageAFixture()
        c = fixture.snapshot.candidate
        report = ReviewReport(review_id="runtime-review", run_id=c.run_id, reviewer_role_run_id=fixture.reviewer.role_run_id,
            candidate_id=c.candidate_id, source_tree_digest=c.source_tree_digest, verdict="approve", coverage=("Offline scripted only",))
        class ScriptedReview:
            usage = []
            contains_credentials = staticmethod(lambda text: False)
            def query(self, messages):
                return {"role": "assistant", "extra": {"actions": [{"tool": "submit_review", "arguments": {"report": report.model_dump(mode="json")}, "tool_call_id": "review-1"}]}}
            format_observation_messages = ProtectedReviewModel.format_observation_messages
        context = SimpleNamespace(check=lambda: None, identity=fixture.reviewer)
        result = run_review(ScriptedReview(), fixture.broker, context, fixture.root / "review.json", "offline")
        self.assertEqual(result.review_id, report.review_id)
        self.assertTrue(fixture.broker.reviews)

    def test_repeated_same_evidence_does_not_continue_indefinitely(self):
        fixture = StageAFixture()
        args = {"snapshot_id": fixture.snapshot.candidate.snapshot_id, "document_id": fixture.doc().document_id, "start": 1, "end": 2}
        class Repeated:
            usage = []
            contains_credentials = staticmethod(lambda text: False)
            def query(self, messages):
                return {"role": "assistant", "extra": {"actions": [{"tool": "read_lines", "arguments": args, "tool_call_id": str(len(messages))}]}}
            format_observation_messages = ProtectedReviewModel.format_observation_messages
        with self.assertRaisesRegex(ProtocolError, "review_repeated_evidence_limit"):
            run_review(Repeated(), fixture.broker, SimpleNamespace(check=lambda: None, identity=fixture.reviewer), fixture.root / "repeated.json", "offline")
        self.assertFalse(fixture.broker.reviews)

    def test_repair_continuation_closes_real_submit_without_reexecuting_it(self):
        model = DeterministicToolcallModel(outputs=[make_toolcall_output("first",
            [{"id": "first", "type": "function", "function": {"name": "bash", "arguments": '{"command":"submit"}'}}], [{"command": "submit", "tool_call_id": "first"}]),
            make_toolcall_output("second", [{"id": "second", "type": "function", "function": {"name": "bash", "arguments": '{"command":"submit"}'}}],
            [{"command": "submit", "tool_call_id": "second"}])])
        class Submitting(LocalEnvironment):
            completed_submission = None
            calls = 0
            def execute(self, action, cwd="", **kwargs):
                self.calls += 1
                message = {"role": "exit", "content": "done", "extra": {"exit_status": "Submitted", "submission": "actual completion"}}
                self.completed_submission = {"action": dict(action), "message": message}
                raise Submitted(message)
        env = Submitting()
        agent = DefaultAgent(model, env, system_template="test", instance_template="{{task}}", output_path=self.store.home / "first.json")
        session = RepairSession(agent, self.context)
        self.assertEqual(session.start("original-task")["exit_status"], "Submitted")
        n_calls = agent.n_calls
        (self.store.home / "runs" / self.parent["id"]).mkdir(parents=True)
        session.continue_with_feedback(self.context, "verified-feedback")
        self.assertEqual(env.calls, 2)
        self.assertEqual(agent.n_calls, n_calls + 1)
        self.assertEqual(agent.messages[1]["content"], "original-task")
        receipts = [m for m in agent.messages if m.get("extra", {}).get("completed_submission_receipt")]
        self.assertEqual(receipts[0]["tool_call_id"], "first")

    def test_unproven_submission_cannot_resume(self):
        agent = SimpleNamespace(messages=[{"role": "assistant"}, {"role": "exit", "extra": {"exit_status": "Submitted"}}],
                                env=SimpleNamespace(completed_submission=None))
        with self.assertRaisesRegex(ProtocolError, "cannot_prove"):
            RepairSession(agent, self.context).continue_with_feedback(self.context, "feedback")

    def test_interrupted_live_identity_never_reenters_runner(self):
        parent = self.store.create_collaboration("split-after-boundary", "adaptive_history", "live-offline", {}, execution="live")
        self.store.claim_parent(parent["id"])
        self.store.reconcile_collaborations()
        self.assertEqual(self.store.get(parent["id"])["status"], "interrupted")
        from collaboration_live import run_live_worker
        with patch("collaboration_live.LiveRunner") as runner:
            self.assertEqual(run_live_worker(self.store, parent["id"])["status"], "interrupted")
        runner.assert_not_called()

    def test_real_files_and_grader_complete_feedback_with_scripted_transport(self):
        settings = {k: v for k, v in self.settings.items() if k.startswith("DEEPSEEK")}
        settings["DEEPSEEK_API_KEY"] = "offline-unique-" + uuid.uuid4().hex
        parent = self.store.create_collaboration("split-after-boundary", "adaptive_history", "offline-full-wire",
            {"parent_seconds": 120, "role_seconds": {p: 50 for p in ("repairing", "reviewing", "revising", "final_review")}}, execution="live")
        runner = LiveRunner(self.store, parent["id"], settings=settings, accounting=self.accounting)
        runner.setup()
        counts = {}
        def scripted_transport(**kwargs):
            role = next(r for r in self.store.roles(parent["id"]) if r["status"] == "running")
            phase = role["phase"]
            counts[phase] = counts.get(phase, 0) + 1
            if role["role"] == "repairer":
                if counts[phase] == 2:
                    return response("bash", {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})
                old = "            if maxsplit == 1:\n                yield list(it)\n"
                partial = "            if maxsplit == 1:\n                rest = list(it)\n                if rest and any(rest):\n                    yield rest\n"
                fixed = "            if maxsplit == 1:\n                rest = list(it)\n                if rest:\n                    yield rest\n"
                code = "from pathlib import Path; p=Path('more_itertools/more.py'); s=p.read_text(); p.write_text(s.replace(" + repr(old if phase == "repairing" else partial) + "," + repr(partial if phase == "repairing" else fixed) + "))"
                if phase == "revising":
                    code += "; import json; Path('temp/finding_responses.json').write_text(json.dumps([{'finding_id':'zero-remainder','disposition':'accepted','reason':'Scripted fixture correction; independently checked afterwards','evidence_ids':[]}]))"
                return response("bash", {"command": "python -B -c " + shlex.quote(code)})
            initial = json.loads(kwargs["messages"][1]["content"].split("\n", 1)[1])
            if phase == "reviewing" and counts[phase] == 1:
                doc = next(d for d in initial["documents"] if d["side"] == "candidate" and d["path"] == "more_itertools/more.py")
                return response("read_lines", {"snapshot_id": initial["snapshot_id"], "document_id": doc["document_id"], "start": 1427, "end": 1433})
            report = {k: initial[k] for k in ("run_id", "reviewer_role_run_id", "candidate_id", "source_tree_digest", "review_id")}
            report.update(verdict="request_changes" if phase == "reviewing" else "approve", coverage=["Scripted review, not a real model"], findings=[])
            if phase == "reviewing":
                ev = json.loads(kwargs["messages"][-1]["content"])["evidence"]["evidence_id"]
                report["findings"] = [{"finding_id": "zero-remainder", "severity": "major", "category": "behavior",
                    "claim": "A nonempty zero remainder must be preserved.", "evidence_ids": [ev], "requirement_ref": initial["requirements"][0]["requirement_id"],
                    "impact": "Scripted edge-case concern; independent tests assess code."}]
            return response("submit_review", {"report": report})
        with patch("litellm.completion", side_effect=scripted_transport):
            result = CollaborationCoordinator(self.store, runner).run(parent["id"])
        self.assertEqual(result["status"], "finished", result)
        self.assertEqual(counts, {"repairing": 2, "reviewing": 2, "revising": 2, "final_review": 1})
        runner.finalize(result)
        runner.close()
        final = self.store.get(parent["id"])
        self.assertEqual(final["repair_result"], "passed")
        self.assertEqual(final["review_verdict"], "approve")
        self.assertEqual(len(self.store.records(parent["id"], "response")), 1)
        self.assertEqual(attributed_usage(self.accounting, parent["id"])["attempts"], 7)
        self.assertTrue((runner.output / "patch-replay/report.json").exists())

    def test_owned_grader_timeout_stops_process_and_starts_no_model(self):
        parent = self.store.create_collaboration("split-after-boundary", "adaptive_history", "timeout-command", {}, execution="live")
        runner = LiveRunner(self.store, parent["id"], settings=self.settings, accounting=self.accounting)
        with self.assertRaises(TimeoutError):
            runner.command([sys.executable, "-B", "-c", "import time; time.sleep(60)"], timeout=.1, check=lambda: None)
        self.assertEqual(attributed_usage(self.accounting, parent["id"])["attempts"], 0)

    def test_review_provider_failure_preserves_accepted_patch_without_more_roles(self):
        settings = {k: v for k, v in self.settings.items() if k.startswith("DEEPSEEK")}
        settings["DEEPSEEK_API_KEY"] = "offline-unique-" + uuid.uuid4().hex
        parent = self.store.create_collaboration("split-after-boundary", "adaptive_history", "offline-review-outage", {}, execution="live")
        runner = LiveRunner(self.store, parent["id"], settings=settings, accounting=self.accounting)
        runner.setup()
        phases = []
        def transport(**kwargs):
            role = next(r for r in self.store.roles(parent["id"]) if r["status"] == "running")
            phases.append(role["phase"])
            if role["phase"] == "reviewing":
                raise priority_tests.PriorityTests.error(503)
            if len(phases) == 2:
                return response("bash", {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})
            old = "            if maxsplit == 1:\n                yield list(it)\n"
            fixed = "            if maxsplit == 1:\n                rest = list(it)\n                if rest:\n                    yield rest\n"
            code = "from pathlib import Path; p=Path('more_itertools/more.py'); p.write_text(p.read_text().replace(" + repr(old) + "," + repr(fixed) + "))"
            return response("bash", {"command": "python -B -c " + shlex.quote(code)})
        with patch("litellm.completion", side_effect=transport):
            result = CollaborationCoordinator(self.store, runner).run(parent["id"])
        self.assertEqual(result["status"], "failed")
        runner.finalize(result)
        runner.close()
        final = self.store.get(parent["id"])
        self.assertEqual(final["status"], "failed")  # Terminal status is never revived.
        self.assertEqual(final["repair_result"], "passed")
        self.assertEqual(final["collaboration_result"], "degraded")
        self.assertEqual(final["review_verdict"], "unavailable")
        self.assertEqual(phases, ["repairing", "repairing", "reviewing"])
        self.assertEqual([r["status"] for r in self.store.roles(parent["id"])], ["succeeded", "failed", "skipped", "skipped"])
        usage = attributed_usage(self.accounting, parent["id"])
        self.assertEqual(usage["attempts"], 3)
        self.assertEqual(usage["unknown_cost_attempts"], 1)

    def test_live_metadata_cannot_resurrect_or_rewrite_terminal_status(self):
        parent = self.store.create_collaboration("split-after-boundary", "adaptive_history", "immutable-live", {}, execution="live")
        self.store.stop(parent["id"], "failed", "offline failure")
        with self.assertRaises(ProtocolError):
            self.store.attach_live_result(parent["id"], "receipts", {"status": "running"})
        fields = {"metrics": {"attempts": 0}, "artifacts": {}}
        self.store.attach_live_result(parent["id"], "receipts", fields)
        with self.assertRaises(ProtocolError):
            self.store.attach_live_result(parent["id"], "receipts", fields | {"metrics": {"attempts": 1}})
        self.assertEqual(self.store.get(parent["id"])["status"], "failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
