"""Keep src-layout tasks isolated from installed package versions."""
from pathlib import Path
import tempfile
import unittest

from task_workspace import ROOT, package_root


class ImportRootTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="import_root_", dir=ROOT / "temp"))
        (self.repo / "src").mkdir()

    def test_legacy_root_layout_is_preserved(self):
        self.assertEqual(package_root(self.repo, {}), self.repo.resolve())

    def test_src_layout_uses_this_checkout(self):
        self.assertEqual(package_root(self.repo, {"import_root": "src"}), (self.repo / "src").resolve())

    def test_parent_absolute_and_missing_roots_are_rejected(self):
        for value in ["../", str(ROOT), "missing"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                package_root(self.repo, {"import_root": value})


if __name__ == "__main__":
    unittest.main(verbosity=2)
