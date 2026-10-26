import unittest
from boltons.strutils import iter_splitlines,indent
class WithheldCases(unittest.TestCase):
 def test_paragraph_and_boundary_separators(self):self.assertEqual(list(iter_splitlines('\u2029a\u2028')),['','a',''])
 def test_dates_and_mixed_lines(self):self.assertEqual(list(iter_splitlines('March 29\r\nnext\u2029last')),['March 29','next','last'])
 def test_indent_uses_real_line_boundaries(self):self.assertEqual(indent('Feb 28\u2028next','  '),'  Feb 28\n  next')
 def test_empty(self):self.assertEqual(list(iter_splitlines('')),[])

