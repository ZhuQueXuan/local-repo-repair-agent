import unittest
from boltons.urlutils import parse_qsl
class WithheldCases(unittest.TestCase):
 def test_non_ascii_keys_and_values(self):self.assertEqual(parse_qsl('%E9=%F1',encoding='latin-1'),[('é','ñ')])
 def test_plus_and_order(self):self.assertEqual(parse_qsl('a+b=%E9&x=%F1',encoding='latin-1'),[('a b','é'),('x','ñ')])
 def test_blank_options(self):
  self.assertEqual(parse_qsl('%E9=&x=%F1',encoding='latin-1'),[('é',None),('x','ñ')]);self.assertEqual(parse_qsl('%E9=&x=%F1',keep_blank_values=False,encoding='latin-1'),[('x','ñ')])

