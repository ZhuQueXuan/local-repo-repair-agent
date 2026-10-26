import unittest
import click
class PublicCases(unittest.TestCase):
 def test_eof_cause(self):
  original=EOFError('diagnostic')
  @click.command()
  def cli():raise original
  with self.assertRaises(click.Abort) as caught:cli.main([],standalone_mode=False)
  self.assertIs(caught.exception.__cause__,original)
 def test_normal_callback(self):
  @click.command()
  def cli():return 7
  self.assertEqual(cli.main([],standalone_mode=False),7)

