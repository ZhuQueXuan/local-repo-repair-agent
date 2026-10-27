import io,unittest
from boltons.jsonutils import JSONLIterator
DATA=b'{"i":0}\n{"i":1}\n{"i":2}\n{"i":3}\n'
class Cases(unittest.TestCase):
 def test_binary_middle_seek_matches_text(self):
  a=list(JSONLIterator(io.BytesIO(DATA),rel_seek=0.3));b=list(JSONLIterator(io.StringIO(DATA.decode()),rel_seek=0.3))
  self.assertEqual(a,b);self.assertEqual(a,[{'i':2},{'i':3}])
 def test_explicit_endpoint_forward_and_reverse(self):
  self.assertEqual(list(JSONLIterator(io.BytesIO(DATA),rel_seek=1.0)),[])
  self.assertEqual(list(JSONLIterator(io.BytesIO(DATA),rel_seek=1.0,reverse=True)),[{'i':3},{'i':2},{'i':1},{'i':0}])
 def test_negative_seek_equals_position_from_end(self):
  a=list(JSONLIterator(io.BytesIO(DATA),rel_seek=-0.7));b=list(JSONLIterator(io.BytesIO(DATA),rel_seek=0.3));self.assertEqual(a,b)
