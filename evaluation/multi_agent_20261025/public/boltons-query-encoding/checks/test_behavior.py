import unittest
from boltons.urlutils import parse_qsl
class PublicCases(unittest.TestCase):
 def test_latin_value(self):self.assertEqual(parse_qsl('k=%E9',encoding='latin-1'),[('k','é')])
 def test_default_utf8(self):self.assertEqual(parse_qsl('k=%C3%A9'),[('k','é')])

