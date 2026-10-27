import unittest
from datetime import datetime
from boltons.timeutils import isoparse
class Cases(unittest.TestCase):
 def test_fraction_lengths_and_leading_zeroes(self):
  for part,micro in [('1',100000),('01',10000),('000001',1),('00001',10),('00420',4200),('0000001',0)]:
   with self.subTest(part=part):self.assertEqual(isoparse('2020-02-29T23:59:58.'+part),datetime(2020,2,29,23,59,58,micro))
 def test_truncation_never_rounds_or_carries(self):
  self.assertEqual(isoparse('2020-12-31T23:59:59.999999999999'),datetime(2020,12,31,23,59,59,999999))
  self.assertEqual(isoparse('2000-01-01T00:00:00.100000000000'),datetime(2000,1,1,0,0,0,100000))
 def test_existing_inputs_roundtrip_and_calendar_validation(self):
  self.assertEqual(isoparse('2024-03-05'),datetime(2024,3,5))
  self.assertEqual(isoparse('2024-03-05T06:07:08'),datetime(2024,3,5,6,7,8))
  for micro in (0,1,120000,851000,999999):
   dt=datetime(2024,3,5,6,7,8,micro);self.assertEqual(isoparse(dt.isoformat()),dt)
  with self.assertRaises(ValueError):isoparse('2023-02-29T00:00:00.1')
