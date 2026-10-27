import unittest
from boltons.namedutils import namedtuple, namedlist
class Cases(unittest.TestCase):
 def test_empty_type(self):
  for factory in (namedtuple,namedlist):
   with self.subTest(factory=factory.__name__),self.assertRaises(ValueError): factory('', ['x'])
 def test_empty_field(self):
  for factory in (namedtuple,namedlist):
   with self.subTest(factory=factory.__name__),self.assertRaises(ValueError): factory('Point',['x',''])
