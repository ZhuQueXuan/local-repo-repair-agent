import unittest
import click
from click.testing import CliRunner
class Cases(unittest.TestCase):
 def test_direct_empty_default(self):
  @click.command()
  @click.option('--value',default='',show_default=True)
  def cli(value): pass
  result=CliRunner().invoke(cli,['--help'])
  self.assertEqual(result.exit_code,0);self.assertIn('[default: ""]',result.output)
 def test_context_empty_default(self):
  @click.command(context_settings={'show_default':True})
  @click.option('--value',default='')
  def cli(value): pass
  result=CliRunner().invoke(cli,['--help'])
  self.assertEqual(result.exit_code,0);self.assertIn('[default: ""]',result.output)
