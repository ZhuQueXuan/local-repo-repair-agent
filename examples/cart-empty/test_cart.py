import unittest

from cart import cart_total


class CartTotalTests(unittest.TestCase):
    def test_empty_cart(self):
        self.assertEqual(cart_total([]), 0)

    def test_single_item(self):
        self.assertEqual(cart_total([12]), 12)

    def test_multiple_items(self):
        self.assertEqual(cart_total([12, 8, 5]), 25)

    def test_zero_price(self):
        self.assertEqual(cart_total([0, 0]), 0)

    def test_decimal_prices(self):
        self.assertAlmostEqual(cart_total([1.25, 2.5]), 3.75)


if __name__ == "__main__":
    unittest.main()
