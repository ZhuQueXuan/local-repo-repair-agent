import unittest
from boltons.listutils import BarrelList


class LargeSortChecks(unittest.TestCase):
    def test_large_descending_input_sorts_globally(self):
        data = list(range(40000, 0, -1))
        values = BarrelList(data)
        values.insert(0, 0)
        self.assertIsNone(values.sort())
        self.assertEqual(list(values), sorted(data + [0]))
        self.assertEqual(len(values), len(data) + 1)

    def test_large_repeated_values_preserve_multiplicity(self):
        data = [value % 37 for value in range(40000, 0, -1)]
        values = BarrelList(data)
        values.insert(0, 3)
        values.sort()
        self.assertEqual(list(values), sorted(data + [3]))

    def test_sort_then_mutate_and_sort_again(self):
        values = BarrelList(range(40000, 0, -1))
        values.insert(0, 0)
        values.sort()
        values.append(-1)
        self.assertEqual(values.pop(), -1)
        values.sort()
        self.assertEqual(values[0], 0)
        self.assertEqual(values[-1], 40000)

    def test_empty_sort(self):
        values = BarrelList()
        self.assertIsNone(values.sort())
        self.assertEqual(list(values), [])

    def test_small_sort(self):
        values = BarrelList([4, -1, 4, 0])
        values.sort()
        self.assertEqual(list(values), [-1, 0, 4, 4])


if __name__ == "__main__":
    unittest.main(verbosity=2)
