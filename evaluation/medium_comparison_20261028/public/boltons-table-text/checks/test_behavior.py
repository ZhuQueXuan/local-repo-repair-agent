import unittest
from boltons.tableutils import Table
class Cases(unittest.TestCase):
 def test_empty_and_header_only_tables(self):
  self.assertEqual(Table([]).to_text(),'')
  self.assertEqual(Table([],headers=['name']).to_text(),'name\n----')
 def test_nonstring_headers(self):
  text=Table([[7,8]],headers=[None,42]).to_text()
  self.assertEqual([s.strip() for s in text.splitlines()[0].split(' | ')],['None','42'])
 def test_short_headers_do_not_drop_data(self):
  text=Table([[1,2,3],[4,5]],headers=['a']).to_text()
  self.assertEqual([s.strip() for s in text.splitlines()[0].split(' | ')],['a','',''])
  self.assertEqual([s.strip() for s in text.splitlines()[-1].split(' | ')],['4','5','None'])
