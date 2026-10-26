import unittest
from boltons.setutils import IndexedSet


class RemovalSliceTests(unittest.TestCase):
    def values(self):
        values = IndexedSet(range(10))
        values.pop(2)
        return values

    def test_positive_bounds_after_removal(self):
        self.assertEqual(list(self.values()[1:4]), [1, 3, 4])

    def test_negative_start_after_removal(self):
        self.assertEqual(list(self.values()[-3:]), [7, 8, 9])

    def test_step_after_removal(self):
        values = self.values()
        self.assertEqual(list(values[1:8:2]), list(values)[1:8:2])

    def test_direct_iter_slice_after_removal(self):
        values = self.values()
        self.assertEqual(list(values.iter_slice(1, 4)), [1, 3, 4])

    def test_multiple_removals(self):
        values = IndexedSet(range(24))
        for item in (2, 7, 12):
            values.remove(item)
        for bounds in (slice(1, 15), slice(-8, -1), slice(0, 20, 3)):
            with self.subTest(bounds=bounds):
                self.assertEqual(list(values[bounds]), list(values)[bounds])

    def test_very_negative_bounds(self):
        values = self.values()
        self.assertEqual(list(values[-100:4]), list(values)[-100:4])

    def test_bound_combinations(self):
        values = self.values()
        reference = list(values)
        for start in (None, -20, -3, 0, 2, 8, 20):
            for stop in (None, -20, -2, 0, 4, 9, 20):
                for step in (None, 1, 2, 3):
                    bounds = slice(start, stop, step)
                    with self.subTest(bounds=bounds):
                        self.assertEqual(list(values[bounds]), reference[bounds])

    def test_no_removal_and_empty(self):
        for values in (IndexedSet(range(6)), IndexedSet()):
            self.assertEqual(list(values[1:4]), list(values)[1:4])
            self.assertEqual(list(values[-3:]), list(values)[-3:])

    def test_existing_index_and_membership(self):
        values = self.values()
        self.assertEqual(values[2], 3)
        self.assertEqual(values[-1], 9)
        self.assertNotIn(2, values)
        self.assertEqual(list(values), [0, 1, 3, 4, 5, 6, 7, 8, 9])

    def test_existing_negative_step_contract(self):
        self.assertEqual(list(IndexedSet(range(10))[2:4:-1]), [7, 6])
