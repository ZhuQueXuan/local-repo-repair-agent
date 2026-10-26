import unittest
from requests import Request
from requests.utils import super_len
class PublicCases(unittest.TestCase):
 def test_emoji_prepared_length(self):
  body='😀';p=Request('POST','https://example.invalid/',data=body).prepare();self.assertEqual(int(p.headers['Content-Length']),len(body.encode('utf-8')))
 def test_ascii_unchanged(self):self.assertEqual(super_len('plain'),5)

