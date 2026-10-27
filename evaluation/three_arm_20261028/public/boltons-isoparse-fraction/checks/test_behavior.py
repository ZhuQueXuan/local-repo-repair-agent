import unittest
from boltons.timeutils import isoparse
class Cases(unittest.TestCase):
 def test_millisecond_fraction(self):
  self.assertEqual(isoparse('2024-03-05T06:07:08.125').microsecond,125000)
 def test_long_fraction_truncates(self):
  self.assertEqual(isoparse('2024-03-05T06:07:08.123456789').microsecond,123456)
