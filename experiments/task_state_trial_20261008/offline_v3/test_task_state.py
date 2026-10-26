"""Current-task evidence and conservative guard checks, without model requests."""
import hashlib
import json
from pathlib import Path
import sys
import unittest
import uuid

sys.dont_write_bytecode = True
from task_state import TaskState
from tool_output_budget import ToolOutputBudget
from context_budget import ContextBudget, message_bytes
from run_task_batch import StatefulModel, StatefulTaskEnvironment
from state_agent import NoProgress
from minisweagent.models.litellm_model import LitellmModelConfig

ROOT = Path(__file__).resolve().parent.parent


class StateTests(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / "temp/task_state_tests" / uuid.uuid4().hex
        self.work.mkdir(parents=True)
        (self.work / "code.py").write_text("print('before')\n", encoding="utf-8")
        self.state = TaskState(self.work, ["code.py"], guard_enabled=True)
        self.output = ToolOutputBudget(self.work)
        self.command = "sed -n '1,20p' code.py"

    def observe(self, command=None, text="same source", code=0):
        command = command or self.command
        output = {"output": text, "returncode": code, "exception_info": ""}
        result = self.output.process(output, command=command)
        self.state.observe(command, output, result["extra"]["tool_output"])

    def repeat(self):
        for _ in range(3):
            self.assertIsNone(self.state.before_action(self.command))
            self.observe()

    def test_same_read_warns_then_blocks_then_stops(self):
        self.repeat()
        self.assertIn("Next repeat", self.state.warning)
        decisions = [self.state.before_action(self.command) for _ in range(3)]
        self.assertEqual([d["stop"] for d in decisions], [False, False, True])
        self.assertEqual(self.state.facts()["executed_actions"], 3)
        self.assertEqual(self.state.facts()["blocked_reads"], 3)
        self.assertIn("temp/tool-output", decisions[0]["output"]["output"])

    def test_source_edit_resets_guard_and_tracks_changes(self):
        self.repeat()
        self.state.before_action(self.command)
        (self.work / "code.py").write_text("print('fixed')\n", encoding="utf-8")
        self.assertIsNone(self.state.before_action(self.command))
        self.observe(text="fixed source")
        self.assertEqual(self.state.blocked_since_change, 0)
        self.assertEqual(self.state.facts()["changed_files"], ["code.py"])

    def test_same_command_with_changed_output_does_not_accumulate(self):
        for text in ["a", "b", "c", "d"]:
            self.assertIsNone(self.state.before_action(self.command))
            self.observe(text=text)
        self.assertEqual(next(iter(self.state.repeats.values()))["count"], 1)

    def test_same_output_different_exit_code_resets_read_count(self):
        self.observe(code=0)
        self.observe(code=1)
        self.observe(code=0)
        self.assertIsNone(self.state.before_action(self.command))

    def test_tests_and_writes_are_not_blocked(self):
        for command in ["python -B -m unittest discover -s task_checks -v", "sed -i 's/a/b/' code.py", "cat code.py > copy.py"]:
            for _ in range(8):
                self.assertIsNone(self.state.before_action(command))
                self.observe(command=command)

    def test_normalized_windows_git_prefix_detected(self):
        self.repeat()
        for prefix in ['git rev-parse --show-toplevel', 'git rev-parse --show-toplevel 2>/dev/null || pwd']:
            command = f'set -o pipefail\ncd "$({prefix})" && sed -n "1,20p" code.py'
            self.assertIsNotNone(self.state.before_action(command))

    def test_unknown_or_external_reads_not_blocked(self):
        for command in ["cat ../unrelated.txt", "sed -n '1,20p' temp/log.txt", "sed -n '1,20p' code.py; python modify.py"]:
            for _ in range(5):
                self.assertIsNone(self.state.before_action(command))
                self.observe(command=command)

    def test_disabled_guard_does_not_limit_many_requests(self):
        self.state.guard_enabled = False
        for _ in range(60):
            self.assertIsNone(self.state.before_action(self.command))
            self.observe()
        self.assertEqual(self.state.facts()["executed_actions"], 60)

    def test_new_read_does_not_reset_source_progress_guard(self):
        self.repeat()
        self.state.before_action(self.command)
        self.observe(command="sed -n '21,30p' code.py", text="new evidence")
        self.assertEqual(self.state.blocked_since_change, 1)

    def test_notes_are_bounded_and_unverified(self):
        (self.state.directory / "notes.json").write_text(json.dumps({"hypothesis": "中文" * 300,
            "next_action": "run reproduction", "verified": True}, ensure_ascii=False), encoding="utf-8")
        notes = self.state.facts()["model_notes"]
        self.assertIn("unverified", notes["status"])
        self.assertNotIn("verified", notes)
        self.assertLess(len(notes["hypothesis"].encode("utf-8")), 390)
        (self.state.directory / "notes.json").write_text('{"hypothesis": false}', encoding="utf-8")
        self.assertIn("rejected", self.state.facts()["model_notes"]["status"])

    def test_pinned_state_survives_projection_without_changing_history(self):
        self.state.record_intent("Found the issue; next reproduce.")
        self.observe(text="Traceback: issue reproduced", code=1)
        model = StatefulModel.__new__(StatefulModel)
        model.task_state = self.state
        model.context_budget = ContextBudget(target_bytes=12288, compact_completed_exchanges=True)
        model.config = LitellmModelConfig(model_name="deepseek/deepseek-flash")
        # Full recorded history and the request view remain separate.
        messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "original goal"}]
        before = json.dumps(messages)
        prepared = model._prepare_messages_for_api(messages)
        self.assertEqual(prepared[:2], messages)
        self.assertIn("Current task state", prepared[2]["content"])
        self.assertIn("issue reproduced", prepared[2]["content"])
        self.assertEqual(json.dumps(messages), before)
        self.assertEqual(model.context_budget.events[-1]["reserved_bytes"], message_bytes([prepared[2]]) + 2)

    def test_pinned_message_and_evidence_have_a_finite_size(self):
        for index in range(80):
            self.state.record_intent("分析🙂" * 500)
            self.observe(command="python inspect.py " + str(index), text=str(index) + "中文🙂" * 900)
        message = self.state.pinned_message()
        self.assertLessEqual(len(message["content"].encode("utf-8")), 4096)
        self.assertEqual(len(self.state.evidence), 2)
        self.assertEqual(len((self.state.directory / "events.jsonl").read_text().splitlines()), 80)
        record=json.loads((self.state.directory / "requests.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(record['message'],message)
        self.assertEqual(record['sha256'],hashlib.sha256(message['content'].encode('utf-8')).hexdigest())

    def test_historical_274_call_loop_is_detected_without_api(self):
        source = ROOT / "experiments/output_budget_trial_20261007/indexed-set-removal/context_complete/tool-output"
        events = [json.loads(line) for line in (source / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        # A tiny source fixture provides the same tracked path, not a repair answer.
        (self.work / "boltons").mkdir()
        (self.work / "boltons/setutils.py").write_text("unchanged", encoding="utf-8")
        self.state = TaskState(self.work, ["code.py", "boltons/setutils.py"], guard_enabled=True)
        stop_at = None
        blocked = 0
        for index, event in enumerate(events, 1):
            decision = self.state.before_action(event["command"])
            if decision:
                blocked += 1
                if decision["stop"]:
                    stop_at = index
                    break
            else:
                self.observe(event["command"], (source / Path(event["full_output_path"]).name).read_text(encoding="utf-8"), event["returncode"])
        self.assertIsNotNone(stop_at)
        self.assertLess(stop_at, 50)
        (self.work / "historical_replay.json").write_text(json.dumps({"historical_requests": 274,
            "stop_before_action": stop_at, "blocked_reads": blocked, "model_requests": 0,
            "scope": "Fixed actions replay; not a new model success or real cost saving."}), encoding="utf-8")

    def test_environment_returns_blocked_observations_then_agent_exit(self):
        environment = StatefulTaskEnvironment(self.work / "env", Path(r"D:\Git\bin\bash.exe"),
            source_files=["code.py"], state_policy={"guard_enabled": True}, max_output_bytes=1024)
        (self.work / "env/code.py").write_text("source", encoding="utf-8")
        for _ in range(3):
            environment._process_output({"output": "source", "returncode": 0}, command=self.command)
        for _ in range(2):
            result = environment.execute({"command": self.command})
            self.assertEqual(result["returncode"], -1)
            self.assertIn("Repeated read blocked", result["output"])
        with self.assertRaises(NoProgress) as caught:
            environment.execute({"command": self.command})
        self.assertEqual(caught.exception.messages[0]["extra"]["exit_status"], "NoProgress")
        self.assertEqual(environment.task_state.facts()["executed_actions"], 3)
        self.assertEqual(environment.output_budget.statistics()["observations"], 6)

    def test_successful_split_trace_does_not_trigger_read_guard(self):
        source=ROOT/'experiments/output_budget_trial_20261007/split-after-boundary/context_compacted/tool-output'
        (self.work/'more_itertools').mkdir()
        (self.work/'more_itertools/more.py').write_text('fixture source',encoding='utf-8')
        self.state=TaskState(self.work,['code.py','more_itertools/more.py'],guard_enabled=True)
        events=[json.loads(line) for line in (source/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        for event in events:
            self.assertIsNone(self.state.before_action(event['command']))
            self.observe(event['command'],(source/Path(event['full_output_path']).name).read_text(encoding='utf-8'),event['returncode'])
        self.assertEqual(self.state.facts()['blocked_reads'],0)
        self.assertEqual(self.state.facts()['executed_actions'],11)


if __name__ == "__main__":
    unittest.main(verbosity=2)
