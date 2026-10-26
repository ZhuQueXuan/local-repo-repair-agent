"""Regression for a Windows patch export that failed real fresh-checkout replay."""
import difflib
from pathlib import Path
import subprocess
import sys
import unittest
import uuid

sys.dont_write_bytecode = True
from run_task_batch import write_repair_patch

ROOT = Path(__file__).resolve().parent.parent


class PatchReplayTests(unittest.TestCase):
    def test_exported_patch_applies_to_lf_source_on_windows(self):
        work = ROOT / "temp/patch_replay_tests" / uuid.uuid4().hex
        work.mkdir(parents=True)
        before = "# 示例\ndef value():\n    return 1\n"
        after = "# 示例\ndef value():\n    return 2\n"
        source = work / "sample.py"
        source.write_bytes(before.encode("utf-8"))
        patch = work / "repair.diff"
        write_repair_patch(patch, difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                                     fromfile="a/sample.py", tofile="b/sample.py"))
        result = subprocess.run(["git", "-C", str(work), "apply", "--", str(patch)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(b"\r\n", patch.read_bytes())
        self.assertEqual(source.read_text(encoding="utf-8"), after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
