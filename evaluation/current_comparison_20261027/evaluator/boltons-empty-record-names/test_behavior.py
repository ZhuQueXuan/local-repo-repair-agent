import unittest
from boltons.namedutils import namedtuple, namedlist
class Cases(unittest.TestCase):
 def test_empty_type_is_not_renamable(self):
  for factory in (namedtuple,namedlist):
   for fields in ([],['x'],['','x']):
    with self.subTest(factory=factory.__name__,fields=fields),self.assertRaises(ValueError): factory('',fields,rename=True)
 def test_rename_preserves_values_and_fields(self):
  for factory in (namedtuple,namedlist):
   with self.subTest(factory=factory.__name__):
    record=factory('Sample',['x','','x','class'],rename=True)
    self.assertEqual(record._fields,('x','_1','_2','_3'))
    self.assertEqual(tuple(record(1,2,3,4)),(1,2,3,4))
 def test_valid_zero_fields_and_existing_invalid_names(self):
  for factory in (namedtuple,namedlist):
   with self.subTest(factory=factory.__name__):
    self.assertEqual(tuple(factory('Empty',[])()),())
    value=factory('Point','x, y')(x=3,y=4); self.assertEqual((value.x,value.y),(3,4))
    for fields in (['x-y'],['x','x'],['_hidden']):
     with self.assertRaises(ValueError): factory('Bad',fields)
