import unittest
import click
from click.testing import CliRunner
import asyncio
class WithheldCases(unittest.TestCase):
 def test_interrupt_and_eof_subclass(self):
  for original in (KeyboardInterrupt('stop'),asyncio.IncompleteReadError(b'part',9)):
   with self.subTest(kind=type(original).__name__):
    @click.command()
    def cli():raise original
    with self.assertRaises(click.Abort) as caught:cli.main([],standalone_mode=False)
    self.assertIs(caught.exception.__cause__,original)
 def test_standalone_exit_remains(self):
  @click.command()
  def cli():raise EOFError('details')
  result=CliRunner().invoke(cli,[]);self.assertEqual(result.exit_code,1);self.assertIn('Aborted!',result.output);self.assertNotIn('Traceback',result.output)
 def test_unrelated_error_not_wrapped(self):
  original=ValueError('other')
  @click.command()
  def cli():raise original
  with self.assertRaises(ValueError) as caught:cli.main([],standalone_mode=False)
  self.assertIs(caught.exception,original)

