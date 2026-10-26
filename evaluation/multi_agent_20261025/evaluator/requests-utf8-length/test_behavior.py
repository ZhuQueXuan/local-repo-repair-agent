import unittest
from requests import Request
from requests.utils import super_len
from io import BytesIO
class WithheldCases(unittest.TestCase):
 def test_multilingual_direct_lengths(self):
  for body in ('é','中文','e\u0301','a😀中'):
   with self.subTest(body=body):self.assertEqual(super_len(body),len(body.encode('utf-8')))
 def test_prepared_lengths_match_bytes(self):
  for body in ('é中','😀😀'):
   with self.subTest(body=body):
    p=Request('PUT','https://example.invalid/',data=body).prepare();self.assertEqual(int(p.headers['Content-Length']),len(body.encode('utf-8')))
 def test_raw_bytes_and_stream_position(self):
  raw='é中'.encode('utf-8');self.assertEqual(super_len(raw),len(raw));s=BytesIO(raw);s.seek(1);self.assertEqual(super_len(s),len(raw)-1)

