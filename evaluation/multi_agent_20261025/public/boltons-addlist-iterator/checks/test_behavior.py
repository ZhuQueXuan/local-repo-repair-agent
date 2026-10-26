import unittest
from boltons.dictutils import OrderedMultiDict
class PublicCases(unittest.TestCase):
 def test_generator_values(self):
  d=OrderedMultiDict();d.addlist('a',(x for x in [1,2]));self.assertEqual(d.getlist('a'),[1,2]);self.assertEqual(d['a'],2)
 def test_reusable_list(self):
  d=OrderedMultiDict([('a',0)]);d.addlist('a',[1,2]);self.assertEqual(d.items(multi=True),[('a',0),('a',1),('a',2)])

