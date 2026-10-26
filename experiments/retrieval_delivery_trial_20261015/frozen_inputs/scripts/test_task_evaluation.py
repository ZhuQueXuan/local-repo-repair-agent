"""Regression: many failing subtests must not create negative pass counts."""
import sys
import unittest

sys.dont_write_bytecode = True
from check_task import run_suite


class EvaluationCountingTests(unittest.TestCase):
    def test_method_and_subtest_counts_are_separate(self):
        class Sample(unittest.TestCase):
            def test_many_failing_subtests(self):
                for i in range(15):
                    with self.subTest(i=i):
                        self.assertEqual(i, -1)

            def test_passing(self):
                self.assertEqual(1, 1)

        report = run_suite(unittest.defaultTestLoader.loadTestsFromTestCase(Sample))
        self.assertEqual(report["tests_run"], 2)
        self.assertEqual(report["subtests_run"], 15)
        self.assertEqual(len(report["failures"]), 15)
        self.assertEqual(report["passed"], 1)
        self.assertEqual(len(report["failed_test_methods"]), 1)
        self.assertFalse(report["successful"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
