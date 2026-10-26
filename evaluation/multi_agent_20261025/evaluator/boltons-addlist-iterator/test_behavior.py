import unittest
from boltons.dictutils import OrderedMultiDict
from boltons.urlutils import QueryParamDict
class WithheldCases(unittest.TestCase):
 def test_empty_iterator_does_not_create_key(self):
  for cls in (OrderedMultiDict,QueryParamDict):
   with self.subTest(cls=cls.__name__):
    d=cls();d.addlist('a',iter([]));self.assertEqual(len(d),0);self.assertNotIn('a',d);self.assertEqual(d.keys(),[])
 def test_existing_key_and_falsey_values(self):
  for cls in (OrderedMultiDict,QueryParamDict):
   with self.subTest(cls=cls.__name__):
    d=cls([('a','old')]);d.addlist('a',iter([0,False,None]));self.assertEqual(d.getlist('a'),['old',0,False,None]);self.assertEqual(len(d.items(multi=True)),4)
 def test_query_parameter_generator(self):
  d=QueryParamDict();d.addlist('k',(x for x in ['a','b']));self.assertEqual(d.getlist('k'),['a','b']);self.assertEqual(d.to_text(),'k=a&k=b')

