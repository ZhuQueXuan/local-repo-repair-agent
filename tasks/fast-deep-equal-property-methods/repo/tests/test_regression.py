import unittest
from fixture_bridge import evaluate
class Regression(unittest.TestCase):
    def test_plain_and_nested(self):
        self.assertTrue(evaluate({'a':[1,{'b':2}]},{'a':[1,{'b':2}]}))
        self.assertFalse(evaluate({'a':1},{'a':2}))
    def test_array_length_and_order(self):
        self.assertFalse(evaluate([1,2],[2,1]))
        self.assertFalse(evaluate([1],[1,2]))
    def test_dates_and_number_objects(self):
        for kind in ('date','number-object'):
            self.assertTrue(evaluate({'__fixture_kind__':kind,'value':123},{'__fixture_kind__':kind,'value':123}))
            self.assertFalse(evaluate({'__fixture_kind__':kind,'value':123},{'__fixture_kind__':kind,'value':124}))
    def test_regexp_and_nan(self):
        self.assertTrue(evaluate({'__fixture_kind__':'nan'},{'__fixture_kind__':'nan'}))
        self.assertFalse(evaluate({'__fixture_kind__':'regexp','source':'x','flags':'i'},{'__fixture_kind__':'regexp','source':'x','flags':''}))
