import unittest
from boltons.listutils import BarrelList
def segmented():
 value=BarrelList();value.lists=[[0,1,2],[3,4],[5,6,7,8,9]];return value
class Cases(unittest.TestCase):
 def test_clipped_forward_slice(self):
  value=segmented();self.assertEqual(list(value[-50:50:2]),list(range(10))[-50:50:2])
 def test_reverse_slice(self):
  value=segmented();self.assertEqual(list(value[::-1]),list(range(10))[::-1])
 def test_cross_barrel_deletion(self):
  value=segmented();del value[1:8];self.assertEqual(list(value),[0,8,9]);self.assertEqual(len(value),3)
