import unittest
from fixture_bridge import evaluate
class Behavior(unittest.TestCase):
    def test_ordinary_valueof_data_field(self):
        self.assertTrue(evaluate({'valueOf':7,'x':1},{'valueOf':7,'x':1}))
        self.assertFalse(evaluate({'valueOf':7,'x':1},{'valueOf':7,'x':2}))
    def test_null_prototype_maps(self):
        left={'__fixture_kind__':'null-prototype','values':{'x':1}}
        self.assertTrue(evaluate(left,left))
        self.assertFalse(evaluate(left,{'__fixture_kind__':'null-prototype','values':{'x':2}}))
