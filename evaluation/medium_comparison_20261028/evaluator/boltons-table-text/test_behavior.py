import copy,unittest
from boltons.tableutils import Table
class Cases(unittest.TestCase):
 def test_column_width_comes_from_that_column(self):
  t=Table([['abcdefghij','x'],['q','yyy']],headers=['A','B']);lines=t.to_text().splitlines()
  self.assertEqual([len(part) for part in lines[2].split(' | ')],[10,3])
  self.assertEqual([len(part) for part in lines[3].split(' | ')],[10,3])
 def test_empty_headers_and_ragged_data(self):
  text=Table([[1,2],[3]],headers=[]).to_text()
  self.assertEqual(len(text.splitlines()),2)
  self.assertEqual([s.strip() for s in text.splitlines()[-1].split(' | ')],['3','None'])
 def test_headers_can_be_suppressed_and_empty_data_is_safe(self):
  self.assertEqual(Table([],headers=['long']).to_text(with_headers=False),'')
  text=Table([[12,3]],headers=['a','b']).to_text(with_headers=False)
  self.assertEqual(text,'12 | 3');self.assertNotIn('-|-',text)
 def test_maxlen_unicode_and_nonstring_cells(self):
  t=Table([['中文字符更多',123456]],headers=['longheader',0]);text=t.to_text(maxlen=5)
  self.assertEqual([s.strip() for s in text.splitlines()[0].split(' | ')],['lo...','0'])
  self.assertEqual([s.strip() for s in text.splitlines()[-1].split(' | ')],['中文...','12...'])
 def test_repeated_rendering_keeps_table_and_headers(self):
  t=Table([[1,2,3],[4,5]],headers=['one']);before=copy.deepcopy((t.headers,t._data,t._width))
  first=t.to_text();self.assertEqual(t.to_text(),first);t.to_text(with_headers=False,maxlen=5)
  self.assertEqual((t.headers,t._data,t._width),before)
