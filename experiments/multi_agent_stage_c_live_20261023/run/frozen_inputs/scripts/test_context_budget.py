"""Offline protocol, provenance, goal retention and no-fabrication checks."""
import json
from pathlib import Path
import sys
import unittest
import uuid

sys.dont_write_bytecode = True
from context_budget import ContextBudget, message_bytes
from tool_output_budget import ToolOutputBudget

ROOT = Path(__file__).resolve().parent.parent


class ContextTests(unittest.TestCase):
    def test_minimal_compaction_retains_more_history_than_eager_compaction(self):
        options=dict(target_bytes=11000,keep_recent_tool_outputs=4,
                     compact_completed_exchanges=True,retained_audit_entries=3)
        eager=ContextBudget(**options).project(self.messages,self.prepared)
        budget=ContextBudget(**options,compact_to_target=True)
        minimal=budget.project(self.messages,self.prepared)
        count=lambda messages: sum(len(m.get('tool_calls',[])) for m in messages)
        self.assertGreater(count(minimal),count(eager))
        self.assertLessEqual(message_bytes(minimal),11000)
        self.assertEqual(minimal[:2],self.prepared[:2])
        self.assertEqual(minimal[-8:],self.prepared[-8:])
        calls=[c['id'] for m in minimal for c in m.get('tool_calls',[])]
        results=[m['tool_call_id'] for m in minimal if m['role']=='tool']
        self.assertEqual(calls,results)

    def test_minimal_compaction_reserves_state_space(self):
        budget=ContextBudget(target_bytes=12000,keep_recent_tool_outputs=4,
            compact_completed_exchanges=True,retained_audit_entries=3,compact_to_target=True)
        projected=budget.project(self.messages,self.prepared,reserved_bytes=2000)
        self.assertLessEqual(message_bytes(projected)+2000,12000)
        self.assertEqual(budget.events[-1]['reserved_bytes'],2000)

    def setUp(self):
        self.work = ROOT / "temp/context_budget_tests" / uuid.uuid4().hex
        self.work.mkdir(parents=True)
        store = ToolOutputBudget(self.work)
        self.messages = [{"role": "system", "content": "Use a tool and protect tests."},
                         {"role": "user", "content": "Fix the collection slicing issue."}]
        for i in range(10):
            action = {"id": f"call_{i}", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": f"inspect {i}"})}}
            self.messages.append({"role": "assistant", "content": None, "tool_calls": [action]})
            output = store.process({"output": f"result {i}\n" + "reading\n" * 600, "returncode": 7 if i == 0 else 0,
                                    "exception_info": "Command timed out." if i == 0 else ""}, command=f"inspect {i}")
            self.messages.append({"role": "tool", "tool_call_id": f"call_{i}",
                "content": f"<returncode>{output['returncode']}</returncode>\n<output>{output['output']}</output>",
                "extra": output["extra"] | {"returncode": output["returncode"], "exception_info": output["exception_info"]}})
        self.prepared = [{k: v for k, v in message.items() if k != "extra"} for message in self.messages]
        self.budget = ContextBudget(target_bytes=6500, keep_recent_tool_outputs=2)

    def test_projection_meets_target_and_keeps_goal_actions_and_recent_outputs(self):
        projected = self.budget.project(self.messages, self.prepared)
        self.assertLessEqual(message_bytes(projected), 6500)
        self.assertEqual(projected[:2], self.prepared[:2])
        self.assertEqual(projected[-4:], self.prepared[-4:])
        for old, new in zip(self.prepared, projected):
            self.assertEqual(old["role"], new["role"])
            if old["role"] == "assistant":
                self.assertEqual(old, new)
            if old["role"] == "tool":
                self.assertEqual(old["tool_call_id"], new["tool_call_id"])

    def test_saved_history_and_full_logs_are_not_rewritten(self):
        original = json.dumps(self.messages)
        self.budget.project(self.messages, self.prepared)
        self.assertEqual(json.dumps(self.messages), original)
        self.assertIn("result 0", (self.work / "temp/tool-output/0001.txt").read_text())
        self.assertGreater((self.work / "temp/tool-output/0001.txt").stat().st_size, 1024)

    def test_error_status_and_traceable_reference_survive_projection(self):
        projected = self.budget.project(self.messages, self.prepared)
        first = projected[3]["content"]
        self.assertIn("<returncode>7</returncode>", first)
        self.assertIn("Command timed out.", first)
        self.assertIn("temp/tool-output/0001.txt", first)
        self.assertIn("Not a summary", first)
        self.assertNotIn("successful", first)

    def test_below_threshold_returns_identical_prepared_messages(self):
        projected = ContextBudget(target_bytes=50000).project(self.messages, self.prepared)
        self.assertEqual(projected, self.prepared)
        self.assertFalse(self.budget.events)

    def test_outputs_without_archive_metadata_are_never_discarded(self):
        messages = json.loads(json.dumps(self.messages))
        for message in messages:
            message.pop("extra", None)
        projected = self.budget.project(messages, self.prepared)
        self.assertEqual(projected, self.prepared)
        self.assertGreater(message_bytes(projected), 6500)

    def test_original_task_is_never_shortened_even_if_it_cannot_fit(self):
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "goal" * 5000}]
        projected = self.budget.project(messages, messages)
        self.assertEqual(projected, messages)
        self.assertGreater(message_bytes(projected), 16384)

    def test_projection_records_before_after_and_exact_archives(self):
        self.budget.project(self.messages, self.prepared)
        event = self.budget.events[-1]
        self.assertGreater(event["before_bytes"], event["after_bytes"])
        self.assertTrue(event["replaced_outputs"])
        for replacement in event["replaced_outputs"]:
            self.assertTrue((self.work / replacement["full_output_path"]).is_file())

    def test_completed_exchange_compaction_preserves_tool_call_protocol(self):
        budget = ContextBudget(target_bytes=6500, compact_completed_exchanges=True)
        projected = budget.project(self.messages, self.prepared)
        self.assertEqual(projected[:2], self.prepared[:2])
        self.assertEqual(projected[-4:], self.prepared[-4:])
        self.assertLessEqual(message_bytes(projected), 6500)
        called = [call["id"] for m in projected for call in m.get("tool_calls", [])]
        responded = [m["tool_call_id"] for m in projected if m.get("role") == "tool"]
        self.assertEqual(called, responded)
        self.assertEqual(len(called), 2)
        self.assertIn("events.jsonl", projected[2]["content"])
        self.assertIn('"returncode": 7', projected[2]["content"])

    def test_compaction_keeps_unfinished_calls_and_assistant_reasoning(self):
        messages = json.loads(json.dumps(self.messages))
        messages[2]["content"] = "Important explicit reasoning retained."
        messages.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": "pending", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]})
        prepared = [{k: v for k, v in m.items() if k != "extra"} for m in messages]
        projected = ContextBudget(target_bytes=6500, compact_completed_exchanges=True).project(messages, prepared)
        self.assertIn(prepared[2], projected)
        self.assertIn(prepared[3], projected)
        self.assertEqual(projected[-1], prepared[-1])

    def test_long_runs_keep_a_bounded_audit_excerpt_and_all_archives(self):
        budget = ContextBudget(target_bytes=6500, compact_completed_exchanges=True, retained_audit_entries=3)
        projected = budget.project(self.messages, self.prepared)
        self.assertIn('"older_entries_in_audit_log": 5', projected[2]["content"])
        self.assertNotIn('"sequence": 1,', projected[2]["content"])
        self.assertTrue((self.work / "temp/tool-output/0001.txt").is_file())

    def test_old_assistant_text_is_compacted_only_after_archiving(self):
        messages = json.loads(json.dumps(self.messages))
        messages[2]["content"] = "Earlier reasoning with exact evidence. " * 100
        prepared = [{k: v for k, v in m.items() if k != "extra"} for m in messages]
        budget = ContextBudget(target_bytes=6500, compact_completed_exchanges=True)
        budget.enable_assistant_archives(self.work)
        projected = budget.project(messages, prepared)
        self.assertLessEqual(message_bytes(projected), 6500)
        self.assertEqual(projected[:2], prepared[:2])
        self.assertNotIn(prepared[2], projected)
        path = self.work / "temp/context-history/0002.txt"
        self.assertEqual(path.read_bytes(), prepared[2]["content"].encode("utf-8"))
        self.assertIn("temp/context-history/0002.txt", projected[2]["content"])
        self.assertEqual(messages[2]["content"], prepared[2]["content"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
