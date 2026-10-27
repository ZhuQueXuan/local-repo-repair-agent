"""Progress, conservative validation parsing, integration and fixed trace replay."""
import json
from pathlib import Path
import unittest
import uuid

from execution_progress import ExecutionProgress, runner_tokens, declared_test_chain
from task_state import TaskState
from tool_output_budget import ToolOutputBudget

ROOT = Path(__file__).resolve().parent.parent
COMMANDS = {"targeted": "python -B -m pytest tests/test_code.py -q -p no:cacheprovider",
            "issue": "python -B -m unittest discover -s task_checks -v"}


class ProgressTests(unittest.TestCase):
    def test_declared_compound_checks_require_exact_selections_not_printed_claims(self):
        commands={k:runner_tokens(v) for k,v in COMMANDS.items()}
        chain=COMMANDS['targeted']+'; echo "PYTEST_RC=$?"; '+COMMANDS['issue']+'; echo "UNITTEST_RC=$?"'
        self.assertEqual(declared_test_chain('cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"; '+chain,commands),['targeted','issue'])
        self.assertEqual(declared_test_chain(COMMANDS['targeted']+'; '+COMMANDS['issue'],commands),['targeted','issue'])
        for bad in [chain+'; echo OK',chain.replace('$?','0'),chain.replace('test_code','other'),
                    chain+' || true',chain.replace(COMMANDS['targeted'],'echo '+repr(COMMANDS['targeted'])),
                    'cd /outside && '+chain,chain+'; python modify.py']:
            with self.subTest(command=bad):self.assertIsNone(declared_test_chain(bad,commands))

    def test_click_commands_and_checkpoint_preserve_current_revision_validation(self):
        commands = {"targeted": "PYTHONPATH=src python -B -m pytest tests/test_code.py -q -p no:cacheprovider",
                    "issue": COMMANDS["issue"]}
        progress = ExecutionProgress(revision="before", validation_commands=commands)
        progress.observe({"sequence": 1, "command": commands["targeted"], "output_path": "temp/pass.txt"},
            {"returncode": 0, "output": "3 passed in 0.1s"}, "after")
        saved = json.loads(json.dumps(progress.export_state()))
        restored = ExecutionProgress(revision="after", validation_commands=commands)
        restored.restore_state(saved)
        self.assertEqual(progress.facts(), restored.facts())
        self.assertEqual(restored.facts()["current_validation"]["targeted"], "passed")
        wrong = ExecutionProgress(revision="different", validation_commands=commands)
        with self.assertRaises(ValueError):
            wrong.restore_state(saved)
        self.assertIsNone(runner_tokens("PYTHONPATH=outside python -m pytest tests"))

    def setUp(self):
        self.progress = ExecutionProgress(revision="initial", validation_commands=COMMANDS)
        self.sequence = 0

    def observe(self, command="sed -n '1,20p' code.py", text="source", code=0, revision="initial", exception=""):
        self.sequence += 1
        return self.progress.observe({"sequence": self.sequence, "command": command,
            "output_path": f"temp/tool-output/{self.sequence:04d}.txt"},
            {"output": text, "returncode": code, "exception_info": exception}, revision)

    def test_varied_reads_and_notes_cannot_reset_stagnation(self):
        for i in range(8):
            feedback = self.observe(f"sed -n '{i},{i+1}p' code.py" if i % 2 else "python write_notes.py", text=str(i))
        self.assertTrue(feedback["stagnation_feedback"])
        self.assertIn("specific missing evidence", feedback["next_action"])

    def test_source_edit_changes_plan_and_invalidates_passes(self):
        self.observe(COMMANDS["issue"], "Ran 3 tests in 0.1s\n\nOK\n")
        result = self.observe(revision="edit")
        self.assertEqual(result["current_validation"]["issue"], "stale")
        self.assertEqual(result["phase"], "validate_changes")
        self.assertEqual(result["first_source_change_sequence"], 2)
        self.assertEqual(result["actions_without_source_or_new_validation"], 0)

    def test_failure_updates_plan_and_has_evidence(self):
        result = self.observe(COMMANDS["issue"], "FAIL: test_copy (Checks)\nAssertionError: mismatch\nRan 3 tests in 0.1s\nFAILED (failures=1)\n", 1)
        self.assertEqual(result["phase"], "revise_after_failure")
        self.assertIn("AssertionError: mismatch", result["validation_evidence"]["issue"]["failures"])

    def test_repeated_tests_with_new_timing_do_not_reset(self):
        for i in range(10):
            result = self.observe(COMMANDS["issue"], f"Ran 3 tests in {i}.1s\nOK\n")
        self.assertEqual(result["actions_without_source_or_new_validation"], 9)
        self.assertTrue(result["stagnation_feedback"])

    def test_same_status_different_failure_is_new_evidence(self):
        for name in ["test_a", "test_b"]:
            result = self.observe(COMMANDS["issue"], f"FAIL: {name} (Checks)\nRan 3 tests in 0.1s\nFAILED (failures=1)\n", 1)
            self.assertTrue(result["new_validation_result"])

    def test_exit_zero_or_printed_claim_does_not_count(self):
        result = self.observe(COMMANDS["issue"], "all tests passed")
        self.assertEqual(result["current_validation"]["issue"], "unknown")
        self.assertFalse(result["new_validation_result"])

    def test_no_test_or_all_skipped_does_not_count(self):
        for text in ["Ran 0 tests in 0.1s\nOK\n", "Ran 3 tests in 0.1s\nOK (skipped=3)\n"]:
            result = self.observe(COMMANDS["issue"], text)
            self.assertEqual(result["current_validation"]["issue"], "unknown")

    def test_failed_exit_despite_ok_or_timeout_is_unknown(self):
        for code, exception in [(1, ""), (-1, "Command timed out.")]:
            result = self.observe(COMMANDS["issue"], "Ran 3 tests in 0.1s\nOK\n", code, exception=exception)
            self.assertEqual(result["current_validation"]["issue"], "unknown")

    def test_only_current_required_scopes_allow_review_advice(self):
        self.observe(revision="edit")
        self.observe(COMMANDS["issue"], "Ran 3 tests in 0.1s\nOK\n", revision="edit")
        result = self.observe(COMMANDS["targeted"], "3 passed in 0.1s\n", revision="edit")
        self.assertEqual(result["phase"], "review_and_submit")
        self.assertIn("not independent", result["next_action"])
        result = self.observe(revision="edit2")
        self.assertEqual(result["phase"], "validate_changes")

    def test_wrong_or_filtered_test_selection_does_not_satisfy_scope(self):
        for command in [COMMANDS["targeted"].replace("test_code", "test_other"), COMMANDS["targeted"] + " -k one", "python -m pytest tests/test_code.py::test_one -q"]:
            result = self.observe(command, "1 passed in 0.1s")
            self.assertEqual(result["current_validation"]["targeted"], "not_run")

    def test_shell_prefix_redirection_tail_and_flags(self):
        command = 'set -o pipefail\ncd "$(git rev-parse --show-toplevel)" && ' + COMMANDS["issue"] + " 2>&1 | tail -30"
        self.assertEqual(runner_tokens(command), runner_tokens(COMMANDS["issue"]))
        result = self.observe(command, "Ran 3 tests in 0.1s\nOK\n")
        self.assertEqual(result["current_validation"]["issue"], "passed")

    def test_fake_runner_inside_echo_and_ambiguous_chains_are_declined(self):
        for command in ['echo "python -m unittest discover -s task_checks"', COMMANDS["issue"] + '; echo OK', COMMANDS["issue"] + ' || true', "python - <<'EOF'\nprint('python -m pytest')\nEOF"]:
            self.assertIsNone(runner_tokens(command))

    def test_pytest_failure(self):
        result = self.observe(COMMANDS["targeted"], "FAILED tests/test_code.py::test_a - AssertionError\n1 failed, 2 passed in 0.1s\n", 1)
        self.assertEqual(result["current_validation"]["targeted"], "failed")

    def test_invalid_configuration(self):
        with self.assertRaises(ValueError):
            ExecutionProgress(revision="x", validation_commands=COMMANDS, stagnation_actions=1)
        with self.assertRaises(ValueError):
            ExecutionProgress(revision="x", validation_commands={"issue": "echo passed"})

    def test_integration_ignores_temp_and_protected_changes_and_bounds_request(self):
        work = ROOT / "temp/progress_tests" / uuid.uuid4().hex
        (work / "pkg").mkdir(parents=True)
        (work / "tests").mkdir()
        (work / "pkg/code.py").write_text("before", encoding="utf-8")
        (work / "tests/test.py").write_text("test", encoding="utf-8")
        state = TaskState(work, ["pkg/code.py", "tests/test.py"], implementation_prefix="pkg/",
                          progress_policy={"stagnation_actions": 8}, validation_commands=COMMANDS,
                          include_previews_in_requests=False)
        output_budget = ToolOutputBudget(work)
        for i in range(9):
            (work / "tests/test.py").write_text(str(i), encoding="utf-8")
            state.record_intent("已完成🙂" * 300)
            output = {"output": "read", "returncode": 0}
            archive = output_budget.process(output, command="cat pkg/code.py")["extra"]["tool_output"]
            state.observe("cat pkg/code.py", output, archive)
        self.assertTrue(state.progress.facts()["stagnation_feedback"])
        message = state.pinned_message()["content"]
        self.assertLessEqual(len(message.encode("utf-8")), 4096)
        self.assertIn("execution_progress", message)
        self.assertTrue((state.directory / "progress.jsonl").is_file())

    def test_fixed_historical_actions_get_advice_before_late_repair(self):
        root = ROOT / "experiments/new_task_evaluation_20261010"
        for task, file in [("omd-copy", "dictutils"), ("file-pathlike", "fileutils")]:
            directory = root / task / "adaptive_history"
            state_events = [json.loads(line) for line in (directory / "task-state/events.jsonl").read_text(encoding="utf-8").splitlines()]
            commands = COMMANDS | {"targeted": COMMANDS["targeted"].replace("test_code", "test_" + file)}
            initial = next(event["revision"] for event in state_events if event["kind"] == "executed")
            progress = ExecutionProgress(revision=initial, validation_commands=commands)
            first_feedback = None
            for event in state_events:
                if event["kind"] != "executed":
                    continue
                path = directory / "tool-output" / Path(event["output_path"]).name
                result = progress.observe(event, {"output": path.read_text(encoding="utf-8"),
                    "returncode": event["returncode"], "exception_info": event.get("exception_info", "")}, event["revision"])
                if result["stagnation_feedback"] and first_feedback is None:
                    first_feedback = event["sequence"]
            self.assertEqual(first_feedback, 8)
            if task == "file-pathlike":
                self.assertEqual(progress.first_change, 111)
                self.assertEqual(progress.facts()["current_validation"], {"targeted": "passed", "issue": "passed"})
            else:
                self.assertEqual(progress.facts()["current_validation"], {"targeted": "not_run", "issue": "not_run"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
