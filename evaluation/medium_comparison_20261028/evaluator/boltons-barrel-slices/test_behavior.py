import unittest
from boltons.listutils import BarrelList
def make(data):
 value=BarrelList();value.lists=[data[:3],data[3:6],data[6:]];return value
class Cases(unittest.TestCase):
 def test_read_matrix_matches_list_and_does_not_mutate(self):
  data=[0,1,1,3,4,5,6,7,8]
  for start,stop,step in [(None,None,-2),(-20,20,3),(7,1,-2),(20,-20,-3),(2,2,1),(None,-1,1),(-3,None,1)]:
   with self.subTest(args=(start,stop,step)):
    value=make(data);part=value[slice(start,stop,step)]
    self.assertIsInstance(part,BarrelList);self.assertEqual(list(part),data[slice(start,stop,step)])
    self.assertEqual(list(value.iter_slice(start,stop,step)),list(part));self.assertEqual(list(value),data)
 def test_extended_and_reverse_deletions(self):
  data=list(range(12))
  for sl in [slice(None,None,2),slice(None,None,-2),slice(10,1,-3),slice(-50,50,3)]:
   with self.subTest(slice=sl):
    expected=data[:];del expected[sl];value=make(data);value.del_slice(sl.start,sl.stop,sl.step)
    self.assertEqual(list(value),expected);self.assertEqual(len(value),len(expected))
 def test_endpoints_empty_ranges_and_all(self):
  data=list(range(9))
  for sl in [slice(2,None),slice(None,None),slice(100,200),slice(7,2),slice(-50,2),slice(-2,99)]:
   with self.subTest(slice=sl):
    expected=data[:];del expected[sl];value=make(data);del value[sl];self.assertEqual(list(value),expected)
 def test_zero_step_raises_without_mutation(self):
  data=list(range(9));value=make(data)
  for call in [lambda:list(value.iter_slice(None,None,0)),lambda:value.del_slice(None,None,0),lambda:value[::0]]:
   with self.assertRaises(ValueError): call()
   self.assertEqual(list(value),data)
 def test_followup_mutations_after_empty_and_partial_deletion(self):
  for sl in [slice(None,None),slice(1,8)]:
   expected=list(range(9));value=make(expected);del expected[sl];del value[sl]
   expected.append(40);value.append(40);expected.extend([41,42]);value.extend([41,42])
   self.assertEqual(value.pop(),expected.pop());self.assertEqual(list(value),expected)
   self.assertEqual(list(value[::-1]),expected[::-1])
