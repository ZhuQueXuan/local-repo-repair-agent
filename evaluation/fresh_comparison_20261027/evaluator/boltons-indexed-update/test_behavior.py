import unittest
from boltons.setutils import IndexedSet
class Cases(unittest.TestCase):
 def test_tuple_elements_and_inputs(self):
  value=IndexedSet([(0,1)]); value.update(((2,3),(0,1)),((4,5),)); self.assertEqual(list(value),[(0,1),(2,3),(4,5)])
 def test_empty_and_three_sources(self):
  value=IndexedSet(['a']); value.update([],iter(['b','a']),('c','b')); self.assertEqual(list(value),['a','b','c'])
 def test_existing_call_shapes(self):
  value=IndexedSet([1]); value.update(); value.update(iter([2,1])); self.assertEqual(list(value),[1,2])
