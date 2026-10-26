import unittest
from more_itertools import split_after


class SplitBoundaryTests(unittest.TestCase):
    def test_one_item_at_limit(self):
        self.assertEqual(list(split_after([1], lambda x: x == 1, maxsplit=1)), [[1]])

    def test_limit_at_final_element(self):
        self.assertEqual(list(split_after([1, 2, 3], lambda x: x == 3, maxsplit=1)), [[1, 2, 3]])

    def test_second_split_at_end(self):
        self.assertEqual(list(split_after([1, 2], lambda x: True, maxsplit=2)), [[1], [2]])

    def test_generator_at_end(self):
        self.assertEqual(list(split_after((x for x in [1, 2]), lambda x: x == 2, maxsplit=1)), [[1, 2]])

    def test_none_is_valid_data(self):
        self.assertEqual(list(split_after([None], lambda x: x is None, maxsplit=1)), [[None]])

    def test_nonempty_remainder(self):
        self.assertEqual(list(split_after([1, 2, 3, 4], lambda x: x == 2, maxsplit=1)), [[1, 2], [3, 4]])

    def test_no_match(self):
        self.assertEqual(list(split_after([1, 2], lambda x: False, maxsplit=1)), [[1, 2]])

    def test_empty_input(self):
        self.assertEqual(list(split_after(iter([]), lambda x: True, maxsplit=1)), [])

    def test_zero_splits_contract(self):
        for values in ([], [1, 2]):
            self.assertEqual(list(split_after(iter(values), lambda x: True, maxsplit=0)), [values])

    def test_unlimited_and_unused_limit(self):
        for limit in (-1, 4):
            self.assertEqual(list(split_after([1, 2], lambda x: True, maxsplit=limit)), [[1], [2]])
