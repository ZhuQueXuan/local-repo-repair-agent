import unittest
from boltons.setutils import IndexedSet
class Cases(unittest.TestCase):
 def test_list_inputs(self):
  value=IndexedSet([1]); result=value.update([2,3],[3,4]); self.assertEqual(list(value),[1,2,3,4]); self.assertIsNone(result)
 def test_iterator_inputs(self):
  value=IndexedSet(); value.update(iter([4,2]),iter([2,7])); self.assertEqual(list(value),[4,2,7])
