import unittest
from fixture_bridge import evaluate
class WithheldBehavior(unittest.TestCase):
    def test_tostring_data_fields(self):
        self.assertTrue(evaluate({'toString':'label','n':1},{'toString':'label','n':1}))
        self.assertFalse(evaluate({'toString':'label','n':1},{'toString':'other','n':1}))
    def test_nested_null_prototype_maps(self):
        a={'wrap':[{'__fixture_kind__':'null-prototype','values':{'a':1,'b':[2]}}]}
        self.assertTrue(evaluate(a,a))
        self.assertFalse(evaluate(a,{'wrap':[{'__fixture_kind__':'null-prototype','values':{'a':2,'b':[2]}}]}))
    def test_method_named_nested_array_values(self):
        self.assertTrue(evaluate([{'valueOf':0,'toString':False}],[{'valueOf':0,'toString':False}]))
        self.assertFalse(evaluate([{'valueOf':0}],[{'valueOf':1}]))
    def test_null_maps_with_null_method_data(self):
        a={'__fixture_kind__':'null-prototype','values':{'valueOf':None,'toString':'data','key':'x'}}
        self.assertTrue(evaluate(a,a))
