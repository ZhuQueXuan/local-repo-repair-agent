"""Offline diagnostic acceptance: real copies/processes, scripted transport only."""
import json
from pathlib import Path
import shlex
import sys
import unittest
from unittest.mock import patch
import uuid

import test_collaboration_runtime as runtime
from collaboration import CollaborationCoordinator, attributed_usage
from collaboration_diagnostic import (PROFILE, TASK, SOURCE, SEEDED, FIXED, seed_candidate,
                                      assert_confirmation_unchanged, validate_policy)
from collaboration_live import LiveRunner, verification_summary
from collaboration_panel import create_panel_run, public_policy, collaboration_view
from collaboration_protocol import ProtocolError, canonical_bytes
from task_workspace import ROOT, digest, load_task, prepare_task


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        runtime.RuntimeTests.setUp(self)

    def diagnostic_parent(self):
        return create_panel_run(self.store, {"task": TASK, "profile": "adaptive_history", "mode": "live",
            "collaboration_mode": "dual_review_diagnostic", "policy_version": 1, "request_key": uuid.uuid4().hex})

    def runner(self):
        parent = self.diagnostic_parent()
        settings = {k: v for k, v in self.settings.items() if k.startswith("DEEPSEEK")}
        settings["DEEPSEEK_API_KEY"] = "offline-unique-" + uuid.uuid4().hex
        return LiveRunner(self.store, parent["id"], settings=settings, accounting=self.accounting)

    def test_fixed_selection_and_idempotency_do_not_change_normal_policy(self):
        normal = digest(ROOT / "configs/multi_agent_v1.json")
        parent = self.diagnostic_parent()
        self.assertEqual(parent["collaboration_mode"], "dual_review_diagnostic")
        self.assertIn("Manual", parent["scope"])
        self.assertEqual(parent["collaboration_policy"]["diagnostic_profile"], PROFILE)
        self.assertNotIn("diagnostic_profile", public_policy())
        for args in (("omd-copy", "adaptive_history", "live"), (TASK, "raw", "live"), (TASK, "adaptive_history", "mock")):
            with self.assertRaises(ValueError):
                validate_policy(public_policy(True), *args)
        with self.assertRaises(ValueError):
            validate_policy({"diagnostic_profile": "arbitrary"}, TASK, "adaptive_history", "live")
        body = {"task": TASK, "profile": "adaptive_history", "mode": "live", "collaboration_mode": "dual_review_diagnostic",
                "policy_version": 1, "request_key": uuid.uuid4().hex}
        one = create_panel_run(self.store, body)
        self.assertEqual(one["id"], create_panel_run(self.store, body)["id"])
        with self.assertRaises(Exception):
            create_panel_run(self.store, body | {"collaboration_mode": "dual_review"})
        with self.assertRaises(ValueError):
            create_panel_run(self.store, body | {"seed_path": "anything"})
        self.assertEqual(digest(ROOT / "configs/multi_agent_v1.json"), normal)

    def test_seed_exact_guard_and_confirmation_detect_later_edits(self):
        source, manifest = load_task(TASK)
        before = digest(source / "repo" / SOURCE)
        work, _ = prepare_task(TASK)
        metadata = seed_candidate(work)
        assert_confirmation_unchanged(work, manifest, metadata)
        with self.assertRaisesRegex(ProtocolError, "source_mismatch"):
            seed_candidate(work)
        path = work / SOURCE
        path.write_text(path.read_text(encoding="utf-8").replace(SEEDED, FIXED), encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "changed_source"):
            assert_confirmation_unchanged(work, manifest, metadata)
        self.assertEqual(digest(source / "repo" / SOURCE), before)

    def test_independent_process_checks_bad_and_fixed_candidates_and_import(self):
        work, _ = prepare_task(TASK)
        seed_candidate(work)
        def evaluate(label):
            target = self.store.home / (label + ".json")
            result = runtime.subprocess.run([sys.executable, "-B", str(ROOT / "scripts/collaboration_diagnostic.py"),
                "--repo", str(work), "--output", str(target)], capture_output=True, cwd=ROOT)
            return result.returncode, json.loads(target.read_text(encoding="utf-8"))
        code, bad = evaluate("bad")
        self.assertEqual(code, 1)
        self.assertEqual(bad["failed_cases"], ["zero", "none", "false", "zeros"])
        self.assertTrue(Path(bad["imported_package"]).is_relative_to(work))
        path = work / SOURCE
        path.write_text(path.read_text(encoding="utf-8").replace(SEEDED, FIXED), encoding="utf-8")
        code, fixed = evaluate("fixed")
        self.assertEqual((code, fixed["passed"]), (0, 6))

    def test_report_requires_additional_checks_even_when_old_checks_pass(self):
        runner = self.runner()
        runner.setup()
        bad = json.loads((runner.output / "seed-validation/report.json").read_text(encoding="utf-8"))
        self.assertTrue(bad["task_tests_passed"])
        self.assertEqual(bad["issue_checks"]["passed"], 10)
        self.assertEqual(bad["upstream_targeted"]["passed"], 4)
        self.assertFalse(runner.assessment_passes(bad))
        summary = json.loads(verification_summary(bad, runner.baseline))
        self.assertFalse(summary["accepted_against_baseline"])
        self.assertEqual(summary["diagnostic_checks"]["passed"], 2)

    def test_grader_uses_temp_scratch_then_copies_artifact(self):
        runner = self.runner()
        with patch.object(runner, "command", wraps=runner.command) as command:
            runner.setup()
        calls = [c.args[0] for c in command.call_args_list if "--output" in c.args[0]]
        self.assertEqual(len(calls), 4)
        for args in calls:
            output = Path(args[args.index("--output") + 1])
            self.assertTrue(output.is_relative_to(ROOT / "temp/task_service"))
            self.assertFalse(output.is_relative_to(runner.output))
        self.assertTrue((runner.output / "baseline/report.json").is_file())
        self.assertEqual(attributed_usage(self.accounting, runner.run_id)["attempts"], 0)

    def exercise(self, approve=False, mutate=False):
        runner = self.runner()
        runner.setup()
        counts, first_messages = {}, []
        def transport(**kwargs):
            role = next(r for r in self.store.roles(runner.run_id) if r["status"] == "running")
            phase = role["phase"]
            counts[phase] = counts.get(phase, 0) + 1
            if phase == "repairing":
                self.assertIn("supplied manually", kwargs["messages"][1]["content"])
                first_messages.append(kwargs["messages"][1]["content"])
                if mutate:
                    p = runner.work / SOURCE
                    p.write_text(p.read_text(encoding="utf-8").replace(SEEDED, FIXED), encoding="utf-8")
                return runtime.response("bash", {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})
            if phase == "revising":
                self.assertEqual(kwargs["messages"][1]["content"], first_messages[0])
                self.assertTrue(any(m.get("extra", {}).get("completed_submission_receipt") for m in runner.session.agent.messages))
                if counts[phase] == 2:
                    # Intentionally omit pytest cache flags: the role adapter
                    # must keep tool-created cache inside temp for collection.
                    return runtime.response("bash", {"command": "python -B -m pytest -q task_checks/test_behavior.py"})
                if counts[phase] == 3:
                    return runtime.response("bash", {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})
                code = ("from pathlib import Path; import json; p=Path('more_itertools/more.py'); p.write_text(p.read_text().replace(" + repr(SEEDED) + "," + repr(FIXED) + ")); "
                        "Path('temp/finding_responses.json').write_text(json.dumps([{'finding_id':'falsey','disposition':'accepted','reason':'Offline scripted correction','evidence_ids':[]}]))")
                return runtime.response("bash", {"command": "python -B -c " + shlex.quote(code)})
            initial = json.loads(kwargs["messages"][1]["content"].split("\n", 1)[1])
            if phase == "reviewing" and not approve and counts[phase] == 1:
                return runtime.response("read_report", {"report_id": initial["report_ids"][0]})
            report = {k: initial[k] for k in ("run_id", "reviewer_role_run_id", "candidate_id", "source_tree_digest", "review_id")}
            report.update(verdict="approve" if approve or phase == "final_review" else "request_changes", coverage=["Offline scripted diagnostic"], findings=[])
            if phase == "reviewing" and not approve:
                ev = json.loads(kwargs["messages"][-1]["content"])["evidence"]["evidence_id"]
                report["findings"] = [{"finding_id": "falsey", "severity": "major", "category": "behavior", "claim": "Declared falsey remainder checks fail",
                    "evidence_ids": [ev], "requirement_ref": initial["requirements"][0]["requirement_id"], "impact": "A nonempty remainder is lost"}]
            return runtime.response("submit_review", {"report": report})
        with patch("litellm.completion", side_effect=transport):
            result = CollaborationCoordinator(self.store, runner).run(runner.run_id)
        if mutate:
            self.assertEqual(result["status"], "failed")
            self.assertEqual(counts, {"repairing": 1})
            self.assertFalse(self.store.records(runner.run_id, "report"))
            return
        self.assertEqual(result["status"], "finished", result)
        runner.finalize(result)
        runner.close()
        final = self.store.get(runner.run_id)
        if approve:
            self.assertEqual(counts, {"repairing": 1, "reviewing": 1})
            self.assertEqual(final["repair_result"], "failed")
            self.assertEqual([r["status"] for r in self.store.roles(runner.run_id)][2:], ["skipped", "skipped"])
        else:
            self.assertEqual(counts, {"repairing": 1, "reviewing": 2, "revising": 3, "final_review": 1})
            self.assertEqual(final["repair_result"], "passed")
            self.assertFalse((runner.work / ".pytest_cache").exists())
            self.assertTrue((runner.work / "temp/test-runtime/pytest-cache").is_dir())
            self.assertEqual(len(list((runner.work / "temp/collaboration-feedback").glob("review-*.json"))), 1)
            roles = self.store.roles(runner.run_id)
            self.assertEqual(roles[0]["session_id"], roles[2]["session_id"])
            self.assertEqual(roles[0]["result"]["candidate_origin"], "manual_seed_confirmed_by_model")
            candidates = collaboration_view(self.store, runner.run_id)["candidates"]
            self.assertEqual(len(candidates), 2)
            old = self.store.snapshot(runner.run_id, candidates[0]["candidate_id"])
            doc = next(d for d in old.candidate.documents if d.side == "candidate" and d.path == SOURCE)
            self.assertIn(b"if rest and any(rest):", old.read(doc.document_id))
            replay = json.loads((runner.output / "patch-replay/report.json").read_text(encoding="utf-8"))
            self.assertEqual(replay["diagnostic_checks"]["passed"], 6)
        return runner

    def test_actual_full_feedback_preserves_session_versions_and_replay(self):
        self.exercise()

    def test_real_verdict_is_not_forced_and_approve_starts_no_revision(self):
        self.exercise(approve=True)

    def test_initial_model_mutation_stops_without_restoring_or_retrying(self):
        self.exercise(mutate=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
