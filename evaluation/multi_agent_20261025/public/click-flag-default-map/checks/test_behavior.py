import unittest
import click
from click.testing import CliRunner
class PublicCases(unittest.TestCase):
 def test_context_true_help(self):
  @click.command(context_settings={'default_map':{'long':True}})
  @click.option('--long/--short',default=False,show_default=True)
  def cli(long):pass
  r=CliRunner().invoke(cli,['--help']);self.assertEqual(r.exit_code,0);self.assertIn('[default: long]',r.output)
 def test_no_override(self):
  @click.command()
  @click.option('--long/--short',default=True,show_default=True)
  def cli(long):pass
  self.assertIn('[default: long]',CliRunner().invoke(cli,['--help']).output)

