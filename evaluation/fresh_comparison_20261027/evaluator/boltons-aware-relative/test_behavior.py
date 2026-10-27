import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from boltons import timeutils
class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        current=cls(2026,1,10,12,tzinfo=timezone.utc)
        return current.replace(tzinfo=None) if tz is None else current.astimezone(tz)
class Cases(unittest.TestCase):
 def test_positive_and_negative_offsets(self):
  with patch.object(timeutils,'datetime',FixedDateTime):
   for offset in (timedelta(hours=5,minutes=30),timedelta(hours=-7)):
    with self.subTest(offset=offset):
     value=datetime(2026,1,10,10,tzinfo=timezone.utc).astimezone(timezone(offset)); self.assertEqual(timeutils.decimal_relative_time(value),(2.0,'hours'))
 def test_future_and_rounding(self):
  with patch.object(timeutils,'datetime',FixedDateTime):
   self.assertEqual(timeutils.relative_time(datetime(2026,1,12,12,tzinfo=timezone.utc)),'2 days from now')
   self.assertEqual(timeutils.decimal_relative_time(datetime(2026,1,10,10,30,tzinfo=timezone.utc),ndigits=1,cardinalize=False),(1.5,'hour'))
 def test_naive_and_explicit_comparison(self):
  with patch.object(timeutils,'datetime',FixedDateTime):
   self.assertEqual(timeutils.decimal_relative_time(datetime(2026,1,9,12)),(1.0,'day'))
   first=datetime(2026,1,10,9,tzinfo=timezone.utc); other=datetime(2026,1,10,18,tzinfo=timezone(timedelta(hours=8)))
   self.assertEqual(timeutils.decimal_relative_time(first,other),(1.0,'hour'))
