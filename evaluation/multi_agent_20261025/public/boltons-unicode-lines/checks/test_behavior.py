import unittest
from boltons.strutils import iter_splitlines
class PublicCases(unittest.TestCase):
 def test_ordinary_date(self):self.assertEqual(list(iter_splitlines('February 28, 2026')),['February 28, 2026'])
 def test_line_separator(self):self.assertEqual(list(iter_splitlines('a\u2028b')),['a','b'])

