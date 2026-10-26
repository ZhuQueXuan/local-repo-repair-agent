"""Offline output integrity, failure feedback and fixed-trajectory replay checks."""
import hashlib
import json
from pathlib import Path
import sys
import unittest
import uuid

sys.dont_write_bytecode = True
from tool_output_budget import ToolOutputBudget

ROOT = Path(__file__).resolve().parent.parent


class ToolOutputTests(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / "temp" / "output_budget_tests" / uuid.uuid4().hex
        self.work.mkdir(parents=True)
        self.budget = ToolOutputBudget(self.work, max_output_bytes=1024)

    def output(self, text, code=0, exception=""):
        return self.budget.process({"output": text, "returncode": code, "exception_info": exception}, command="inspect")

    def test_short_output_and_status_are_unchanged(self):
        result = self.output("test result\n", code=7, exception="process failed")
        self.assertEqual(result["output"], "test result\n")
        self.assertEqual(result["returncode"], 7)
        self.assertEqual(result["exception_info"], "process failed")
        self.assertFalse(result["extra"]["tool_output"]["truncated"])

    def test_long_failure_keeps_head_tail_full_file_and_exit_status(self):
        original = "TRACEBACK START\n" + "a long traceback\n" * 1000 + "FAILED: expected 3 got 4\n"
        result = self.output(original, code=1)
        self.assertLessEqual(len(result["output"].encode("utf-8")), 1024)
        self.assertIn("TRACEBACK START", result["output"])
        self.assertIn("FAILED: expected 3 got 4", result["output"])
        self.assertIn("TRUNCATED", result["output"])
        self.assertEqual(result["returncode"], 1)
        record = result["extra"]["tool_output"]
        raw = (self.work / record["full_output_path"]).read_bytes()
        self.assertEqual(raw, original.encode("utf-8"))
        self.assertEqual(hashlib.sha256(raw).hexdigest(), record["sha256"])

    def test_utf8_byte_cap_with_multibyte_text(self):
        original = "开头\n" + "中文🙂é\n" * 2000 + "结尾\n"
        result = self.output(original)
        self.assertLessEqual(len(result["output"].encode("utf-8")), 1024)
        self.assertNotIn("\ufffd", result["output"])
        self.assertIn("开头", result["output"])
        self.assertIn("结尾", result["output"])
        self.assertEqual((self.work / result["extra"]["tool_output"]["full_output_path"]).read_bytes(), original.encode("utf-8"))

    def test_middle_content_can_be_retrieved_without_loss(self):
        original = "\n".join(f"line {i}: unique value {i}" for i in range(400)) + "\n"
        preview = self.output(original)
        full = (self.work / preview["extra"]["tool_output"]["full_output_path"]).read_text(encoding="utf-8")
        self.assertNotIn("line 200:", preview["output"])
        requested = "\n".join(full.splitlines()[198:202])
        result = self.output(requested)
        self.assertIn("line 200: unique value 200", result["output"])
        self.assertFalse(result["extra"]["tool_output"]["truncated"])

    def test_exact_boundary_and_one_byte_over(self):
        self.assertFalse(self.output("x" * 1024)["extra"]["tool_output"]["truncated"])
        self.assertTrue(self.output("x" * 1025)["extra"]["tool_output"]["truncated"])

    def test_timeout_and_original_extra_fields_survive(self):
        result = self.budget.process({"output": "partial " * 800, "returncode": -1,
            "exception_info": "Command timed out.", "extra": {"existing": "preserved"}}, command="long-running tool")
        self.assertEqual(result["returncode"], -1)
        self.assertEqual(result["exception_info"], "Command timed out.")
        self.assertEqual(result["extra"]["existing"], "preserved")

    def test_submission_marker_is_not_hidden(self):
        original = "  COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT  \n" + "submission\n" * 300
        result = self.output(original)
        self.assertEqual(result["output"], original)
        self.assertFalse(result["extra"]["tool_output"]["truncated"])

    def test_archive_does_not_overwrite_previous_output(self):
        self.output("first")
        self.output("second")
        events = [json.loads(line) for line in (self.budget.directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([event["sequence"] for event in events], [1, 2])
        self.assertEqual((self.budget.directory / "0001.txt").read_text(), "first")
        with self.assertRaises(FileExistsError):
            ToolOutputBudget(self.work).process({"output": "replacement", "returncode": 0}, command="inspect")
        self.assertEqual((self.budget.directory / "0001.txt").read_text(), "first")

    def test_same_historical_actions_fit_before_previous_stop(self):
        source = ROOT / "experiments/local_baseline_20261007/indexed-set-removal/trajectory.json"
        trajectory = json.loads(source.read_text(encoding="utf-8"))
        original = [{k: v for k, v in m.items() if k != "extra"} for m in trajectory["messages"][:-1]]
        reduced = json.loads(json.dumps(original))
        for message in reduced:
            if message.get("role") == "tool":
                prefix, rest = message["content"].split("<output>", 1)
                raw, ending = rest.rsplit("</output>", 1)
                preview = self.output(raw)["output"]
                message["content"] = prefix + "<output>" + preview + "</output>" + ending
        original_bytes = len(json.dumps(original, ensure_ascii=False).encode("utf-8"))
        reduced_bytes = len(json.dumps(reduced, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(original_bytes, 16784)
        self.assertGreater(original_bytes, 16384)
        self.assertLess(reduced_bytes, 16384)
        (self.work / "fixed_trace_comparison.json").write_text(json.dumps({"original_bytes": original_bytes,
            "bounded_bytes": reduced_bytes, "same_actions": True, "model_requests": 0}), encoding="utf-8")


if __name__ == "__main__":
    unittest.main(verbosity=2)
