import unittest
from boltons.mathutils import Bits
class Cases(unittest.TestCase):
 def test_constructor_width_boundary(self):
  with self.assertRaises(ValueError): Bits(8,3)
  self.assertEqual(Bits(7,3).as_bin(),'111')
 def test_from_int_width_boundary(self):
  with self.assertRaises(ValueError): Bits.from_int(16,4)
  self.assertEqual(Bits.from_int(0,4).as_bin(),'0000')
