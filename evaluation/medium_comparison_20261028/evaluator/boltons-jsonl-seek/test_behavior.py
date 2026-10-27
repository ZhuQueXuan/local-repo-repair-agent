import io,unittest
from boltons.jsonutils import JSONLIterator
DATA=b'{"x":0}\n{"x":1}\n{"x":2}\n{"x":3}\n{"x":4}\n'
class GuardedBytes(io.BytesIO):
 def __init__(self,data): super().__init__(data);self.reads=0
 def read(self,*args):
  self.reads+=1
  if self.reads>6: raise AssertionError('alignment must terminate at EOF')
  return super().read(*args)
class Cases(unittest.TestCase):
 def test_varied_offsets_forward_and_reverse_match_separator_oracle(self):
  for pos in (0.12,0.35,0.62,0.9):
   target=int(len(DATA)*pos);sep=DATA.find(b'\n',target);sep=len(DATA) if sep<0 else sep
   expected=[__import__('json').loads(line) for line in DATA[sep:].splitlines() if line.strip()]
   prior=[__import__('json').loads(line) for line in DATA[:sep].splitlines() if line.strip()]
   self.assertEqual(list(JSONLIterator(io.BytesIO(DATA),rel_seek=pos)),expected)
   self.assertEqual(list(JSONLIterator(io.BytesIO(DATA),rel_seek=pos,reverse=True)),prior[::-1])
 def test_no_remaining_newline_terminates_and_keeps_stream_open(self):
  stream=GuardedBytes(b'{"x":0}\n{"x":1}')
  self.assertEqual(list(JSONLIterator(stream,rel_seek=0.9)),[]);self.assertFalse(stream.closed)
  stream=GuardedBytes(b'');self.assertEqual(list(JSONLIterator(stream,rel_seek=0.5)),[])
 def test_zero_endpoint_negative_and_invalid_bounds(self):
  for reverse in (False,True):
   for pos in (-1.0,1.01,-2):
    with self.assertRaises(ValueError):JSONLIterator(io.BytesIO(DATA),reverse=reverse,rel_seek=pos)
  self.assertEqual(list(JSONLIterator(io.BytesIO(DATA),rel_seek=0)),[{'x':i} for i in range(5)])
  self.assertEqual(list(JSONLIterator(io.BytesIO(DATA),rel_seek=-0.38)),list(JSONLIterator(io.BytesIO(DATA),rel_seek=0.62)))
 def test_existing_cursor_default_reverse_and_ignore_errors(self):
  stream=io.BytesIO(DATA);stream.seek(8);self.assertEqual(list(JSONLIterator(stream)),[{'x':i} for i in range(1,5)])
  stream=io.BytesIO(DATA);self.assertEqual(list(JSONLIterator(stream,reverse=True)),[{'x':i} for i in reversed(range(5))]);self.assertFalse(stream.closed)
  self.assertEqual(list(JSONLIterator(io.BytesIO(b'{"a":1}\nbad\n{"a":2}\n'),ignore_errors=True)),[{'a':1},{'a':2}])
 def test_alignment_across_multiple_blocks(self):
  data=b'{"s":"'+b'a'*9000+b'"}\n{"s":"end"}\n'
  self.assertEqual(list(JSONLIterator(io.BytesIO(data),rel_seek=0.2)),[{'s':'end'}])
