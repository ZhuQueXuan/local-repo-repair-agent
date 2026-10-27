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
 def test_decimal_aware_default(self):
  with patch.object(timeutils,'datetime',FixedDateTime):
   value=datetime(2026,1,10,9,tzinfo=timezone.utc); self.assertEqual(timeutils.decimal_relative_time(value),(3.0,'hours'))
 def test_text_aware_default(self):
  with patch.object(timeutils,'datetime',FixedDateTime):
   self.assertEqual(timeutils.relative_time(datetime(2026,1,8,12,tzinfo=timezone.utc)),'2 days ago')
