"""A changed candidate or rewritten freeze must fail before model transport."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
from evaluation_freeze import verify_freeze
from task_workspace import ROOT


class FreezeTests(unittest.TestCase):
    def setUp(self):
        # Preserve fixtures under temp; no recursive cleanup.
        self.root = Path(tempfile.mkdtemp(prefix="freeze_check_", dir=ROOT / "temp"))
        (self.root / "candidate.py").write_text("frozen", encoding="utf-8")
        self.manifest = {"version": 1, "files_sha256": {
            "candidate.py": hashlib.sha256(b"frozen").hexdigest()}}
        self.save()

    def save(self):
        (self.root / "freeze.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def test_unchanged_inputs(self):
        report = verify_freeze(self.root, "freeze.json")
        self.assertEqual(report["verified_files"], 1)
        self.assertEqual(verify_freeze(self.root, "freeze.json", expected_digest=report["sha256"]), report)

    def test_changed_candidate(self):
        (self.root / "candidate.py").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "input changed"):
            verify_freeze(self.root, "freeze.json")

    def test_rewritten_manifest(self):
        original = verify_freeze(self.root, "freeze.json")
        self.manifest["note"] = "new declaration"
        self.save()
        with self.assertRaisesRegex(RuntimeError, "manifest changed"):
            verify_freeze(self.root, "freeze.json", expected_digest=original["sha256"])

    def test_escape_paths_and_empty_manifest(self):
        self.manifest["files_sha256"] = {"../outside.py": "any"}
        self.save()
        with self.assertRaises(ValueError):
            verify_freeze(self.root, "freeze.json")
        self.manifest["files_sha256"] = {}
        self.save()
        with self.assertRaises(ValueError):
            verify_freeze(self.root, "freeze.json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
