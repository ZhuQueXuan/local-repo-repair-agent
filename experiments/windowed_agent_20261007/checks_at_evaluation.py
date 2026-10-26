"""Behavioral checks for upstream issue 462, independent of any repair."""
import unittest

from more_itertools import windowed


class EmptyWindowTests(unittest.TestCase):
    def test_empty_tuple(self):
        self.assertEqual(list(windowed((), 2)), [])

    def test_empty_list_width_one(self):
        self.assertEqual(list(windowed([], 1)), [])

    def test_empty_iterator(self):
        self.assertEqual(list(windowed(iter(()), 3)), [])

    def test_empty_with_fill_value(self):
        self.assertEqual(list(windowed([], 2, fillvalue="!")), [])

    def test_empty_with_large_step(self):
        self.assertEqual(list(windowed([], 2, step=4)), [])

    def test_short_nonempty_still_padded(self):
        self.assertEqual(list(windowed([1], 3, fillvalue="!")), [(1, "!", "!")])

    def test_none_is_a_real_item(self):
        self.assertEqual(list(windowed([None], 2, fillvalue="!")), [(None, "!")])

    def test_generator_keeps_sliding(self):
        self.assertEqual(list(windowed(iter([1, 2, 3]), 2)), [(1, 2), (2, 3)])

    def test_zero_width_contract_is_preserved(self):
        self.assertEqual(list(windowed([], 0)), [()])

    def test_invalid_step_is_still_rejected(self):
        with self.assertRaises(ValueError):
            list(windowed([], 2, step=0))


if __name__ == "__main__":
    unittest.main()
