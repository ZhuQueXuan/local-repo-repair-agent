"""Replay the actual oversized wire and verify bounded, versioned feedback."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import test_collaboration_runtime as runtime
from collaboration_feedback import declared_checks, repair_feedback, pytest_environment
from collaboration_protocol import (canonical_bytes, sha256, ProtocolError, ReviewReport,
                                    RoleIdentity, FindingResponse, validate_response)
from collaboration_runtime import RepairEnvironment, RepairModel
from context_budget import message_bytes
from run_task_batch import RecordedModel, ContextManagedModel
from task_service_store import policy
from task_workspace import ROOT
from test_collaboration_protocol import StageAFixture


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        runtime.RuntimeTests.setUp(self)

    def model(self, state):
        return runtime.RuntimeTests.model(self, RepairModel, task_state=state,
            context_policy=policy("adaptive_history")["context_budget"])

    def test_actual_16807_byte_failure_projects_below_unchanged_guard(self):
        archive = ROOT / "experiments/multi_agent_feedback_diagnostic_live_20261024"
        raw = json.loads((archive / "run-readable/revising-trajectory.json").read_text(encoding="utf-8"))
        messages = raw["messages"]
        with zipfile.ZipFile(archive / "actual-workspace.zip") as z:
            pin = json.loads(z.read("temp/task-state/requests.jsonl").decode().splitlines()[-1])["message"]
        model = self.model(SimpleNamespace(pinned_message=lambda: pin))
        work = self.store.home / "projection-work"
        work.mkdir()
        model.context_budget.enable_assistant_archives(work)
        original = canonical_bytes(messages)
        filtered = [m for m in messages if m["role"] != "exit"]
        prepared = RecordedModel._prepare_messages_for_api(model, filtered)
        previous = ContextManagedModel.project_context(model, filtered, prepared, [pin])
        self.assertEqual(message_bytes(previous), 16807)
        projected = model._prepare_messages_for_api(messages)
        self.assertLessEqual(message_bytes(projected), 16384)
        self.assertEqual(model.budget_config["max_messages_utf8_bytes"], 16384)
        self.assertEqual(canonical_bytes(messages), original)
        self.assertEqual(projected[:2], prepared[:2])
        feedback = next(m for m in filtered if m["role"] == "user" and "Continue this same repair session" in m.get("content", ""))
        self.assertTrue(any(m.get("content") == feedback["content"] for m in projected))
        self.assertEqual(projected[-2:], prepared[-2:])
        self.assertTrue(any(e["mode"] == "collaboration_guard_projection" for e in model.context_budget.events))
        with patch("litellm.completion", return_value=runtime.response("bash", {"command": "echo offline"})) as transport:
            model.query(messages)
        self.assertEqual(transport.call_count, 1)
        self.assertLessEqual(message_bytes(transport.call_args.kwargs["messages"]), 16384)
        receipt = next(m for m in messages if m.get("extra", {}).get("completed_submission_receipt"))
        self.assertTrue(any(m.get("tool_call_id") == receipt["tool_call_id"] for m in projected))
        (self.store.home / "actual-wire-replay.json").write_bytes(canonical_bytes({"old_bytes": message_bytes(previous),
            "new_bytes": message_bytes(projected), "guard_bytes": 16384, "raw_history_unchanged": True,
            "transport": "scripted offline response, synthetic ledger only", "events": model.context_budget.events}))

    def test_irreducible_input_still_stops_before_network_or_accounting(self):
        model = self.model(SimpleNamespace(pinned_message=lambda: {"role": "user", "content": "state"}))
        messages = [{"role": "system", "content": "repair"}, {"role": "user", "content": "x" * 20000}]
        with patch("litellm.completion") as transport, self.assertRaises(runtime.LimitsExceeded):
            model.query(messages)
        transport.assert_not_called()
        self.assertEqual(runtime.attributed_usage(self.accounting, self.parent["id"])["attempts"], 0)

    def test_small_request_is_identical_to_existing_projection(self):
        pin = {"role": "user", "content": "state"}
        model = self.model(SimpleNamespace(pinned_message=lambda: pin))
        messages = [{"role": "system", "content": "repair"}, {"role": "user", "content": "original task"}]
        prepared = RecordedModel._prepare_messages_for_api(model, messages)
        expected = ContextManagedModel.project_context(model, messages, prepared, [pin])
        self.assertEqual(model._prepare_messages_for_api(messages), expected)
        self.assertFalse(any(e["mode"] == "collaboration_guard_projection" for e in model.context_budget.events if "mode" in e))

    def test_long_review_is_saved_once_with_complete_ids_version_and_digest(self):
        fixture = StageAFixture()
        old = fixture.review()
        finding = old.findings[0].model_dump(mode="json")
        findings = [finding | {"finding_id": "finding-" + str(i), "claim": "边界问题" * 200,
                    "impact": "Impact " * 150} for i in range(20)]
        review = ReviewReport.parse(old.model_dump(mode="json") | {"findings": findings})
        before = canonical_bytes(review)
        manifest = {"agent_test_command": "python -B -m unittest tests.test_main -v"}
        text = repair_feedback(fixture.work, review, fixture.broker.evidence, manifest)
        self.assertLessEqual(len(text.encode()), 8192)
        index = json.loads(text.splitlines()[-1])
        self.assertTrue(index["previews_only"])
        self.assertEqual(len(index["findings"]), 20)
        path = fixture.work / index["full_review_path"]
        payload = path.read_bytes()
        self.assertEqual(sha256(payload), index["full_review_sha256"])
        self.assertEqual(json.loads(payload)["review"], review.model_dump(mode="json"))
        self.assertEqual(canonical_bytes(review), before)
        self.assertEqual(text, repair_feedback(fixture.work, review, fixture.broker.evidence, manifest))
        self.assertEqual(index["reviewed_candidate_id"], review.candidate_id)
        self.assertIn("use accepted", text)
        self.assertIn("old-candidate evidence cannot justify rejection", text)
        self.assertIn(manifest["agent_test_command"], text)
        self.assertIn("python -B -m unittest discover -s task_checks -v", text)
        self.assertIn("confirmation-only instruction has ended", text)

    def test_tampered_feedback_file_cannot_be_silently_replaced(self):
        fixture = StageAFixture()
        review = fixture.review()
        manifest = {"agent_test_command": "fixed command"}
        text = repair_feedback(fixture.work, review, fixture.broker.evidence, manifest)
        index = json.loads(text.splitlines()[-1])
        (fixture.work / index["full_review_path"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(ProtocolError, "feedback_audit_changed"):
            repair_feedback(fixture.work, review, fixture.broker.evidence, manifest)

    def test_protocol_id_never_becomes_a_windows_stream_or_path(self):
        fixture = StageAFixture()
        review = fixture.review(review_id="review:alternate-stream")
        text = repair_feedback(fixture.work, review, fixture.broker.evidence, {"agent_test_command": "fixed"})
        index = json.loads(text.splitlines()[-1])
        self.assertNotIn(":", index["full_review_path"])
        self.assertTrue((fixture.work / index["full_review_path"]).is_file())
        self.assertEqual(json.loads((fixture.work / index["full_review_path"]).read_text(encoding="utf-8"))["review"]["review_id"], "review:alternate-stream")

    def test_old_evidence_cannot_reject_a_true_old_defect_after_revision(self):
        fixture = StageAFixture()
        review = fixture.review()
        fixture.write(fixture.v2)
        target = fixture.seal("revision")
        identity = RoleIdentity(run_id=fixture.run_id, role_run_id="repairer-1", session_id="repair-session", role="repairer")
        fields = {"response_id": "response-fix", "run_id": fixture.run_id, "responder_role_run_id": identity.role_run_id,
            "review_id": review.review_id, "finding_id": review.findings[0].finding_id, "reviewed_candidate_id": review.candidate_id,
            "target_candidate_id": target.candidate.candidate_id, "disposition": "rejected",
            "reason": "Current code is fixed so the old claim is false", "evidence_ids": list(review.findings[0].evidence_ids)}
        args = (review, fixture.snapshot.candidate, target.candidate, identity, fixture.broker.evidence,
                {d.document_id: d for d in target.candidate.documents}, fixture.broker.reports)
        with self.assertRaises(ProtocolError):
            validate_response(FindingResponse.parse(fields), *args)
        accepted = FindingResponse.parse(fields | {"disposition": "accepted", "evidence_ids": [], "reason": "Old defect acknowledged; actual resolution still needs independent checks"})
        self.assertEqual(validate_response(accepted, *args).disposition, "accepted")
        self.assertNotIn("resolved", accepted.model_dump())

    def test_pytest_cache_moves_to_temp_without_loosening_source_manifest(self):
        fixture = StageAFixture()
        (fixture.work / "temp/test_probe.py").write_text("def test_ok():\n    assert 2 + 2 == 4\n", encoding="utf-8")
        env = RepairEnvironment(fixture.work, Path(runtime.os.environ.get("MINI_TASK_BASH", r"D:\Git\bin\bash.exe")),
            context=self.context, source_files=list(fixture.manifest["source_files_sha256"]), state_policy={}, max_output_bytes=1024)
        env.model = SimpleNamespace(contains_credentials=lambda text: False)
        result = env.execute({"command": "python -B -m pytest -q temp/test_probe.py"})
        self.assertEqual(result["returncode"], 0, result["output"])
        self.assertFalse((fixture.work / ".pytest_cache").exists())
        self.assertTrue((fixture.work / "temp/test-runtime/pytest-cache").is_dir())
        fixture.seal("after-pytest")
        rogue = fixture.work / "unexpected.txt"
        rogue.write_text("still rejected", encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "new_file_outside_manifest"):
            fixture.seal("after-unexpected")

    def test_role_child_environment_does_not_forward_parent_secrets(self):
        fixture = StageAFixture()
        env = RepairEnvironment(fixture.work, Path(runtime.os.environ.get("MINI_TASK_BASH", r"D:\Git\bin\bash.exe")),
            context=self.context, source_files=list(fixture.manifest["source_files_sha256"]), state_policy={}, max_output_bytes=1024)
        env.model = SimpleNamespace(contains_credentials=lambda text: False)
        with patch.dict(runtime.os.environ, {"DEEPSEEK_API_KEY": "not-forwarded-test-value"}):
            result = env.execute({"command": "python -B -c \"import os; print(os.environ.get('DEEPSEEK_API_KEY', 'absent'))\""})
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["output"].strip(), "absent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
