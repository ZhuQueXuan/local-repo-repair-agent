import unittest
from boltons.mathutils import Bits
class Cases(unittest.TestCase):
 def test_powers_of_two_and_fitting_neighbors(self):
  for width in (1,2,5,13,64):
   for factory in (Bits,Bits.from_int):
    with self.subTest(width=width,factory=factory):
     with self.assertRaises(ValueError): factory(1<<width,width)
     value=factory((1<<width)-1,width); self.assertEqual(len(value),width);self.assertEqual(len(value.as_bin()),width)
 def test_converted_strings_and_zero_width(self):
  for text in ('1000','0x8'):
   with self.subTest(text=text),self.assertRaises(ValueError): Bits(text,3)
  with self.assertRaises(ValueError): Bits(1,0)
  self.assertEqual(Bits(0,0).as_int(),0);self.assertEqual(len(Bits(0,0)),0)
 def test_padding_inference_and_negative_value(self):
  self.assertEqual(Bits(3,8).as_bin(),'00000011')
  self.assertEqual(Bits(8).as_bin(),'1000')
  self.assertEqual(Bits('0011').as_bin(),'0011')
  self.assertEqual(Bits.from_hex('0F').as_int(),15)
  with self.assertRaises(ValueError): Bits(-1,4)
