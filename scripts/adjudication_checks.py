"""Trusted checks for the already-public seeded-falsey diagnostic only."""
import unittest
from more_itertools import split_after


class FalseyRemainder(unittest.TestCase):
    def verify(self, values, expected):
        try:
            actual = list(split_after(iter(values), lambda x: x == 1, maxsplit=1))
        except Exception as error:
            self.fail("Declared input raised " + type(error).__name__)
        self.assertEqual(actual, expected)

    def test_zero(self): self.verify([1, 0], [[1], [0]])
    def test_none(self): self.verify([1, None], [[1], [None]])
    def test_false(self): self.verify([1, False], [[1], [False]])
    def test_zeros(self): self.verify([1, 0, 0], [[1], [0, 0]])
    def test_nonempty(self): self.verify([1, 2, 3], [[1], [2, 3]])
    def test_empty_tail(self): self.verify([1], [[1]])
