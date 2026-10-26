import unittest
import click
from click.testing import CliRunner
class WithheldCases(unittest.TestCase):
 def test_context_false_overrides_declared_true(self):
  @click.command(context_settings={'default_map':{'long':False}})
  @click.option('--long/--short','-l/-s',default=True,show_default=True)
  def cli(long):click.echo(str(long))
  r=CliRunner();self.assertIn('[default: short]',r.invoke(cli,['--help']).output);self.assertEqual(r.invoke(cli,[]).output.strip(),'False')
 def test_nested_context(self):
  @click.group(context_settings={'default_map':{'sub':{'enabled':True}}})
  def root():pass
  @root.command()
  @click.option('--enabled/--disabled',default=False,show_default=True)
  def sub(enabled):pass
  self.assertIn('[default: enabled]',CliRunner().invoke(root,['sub','--help']).output)
 def test_declared_false_remains(self):
  @click.command()
  @click.option('--enabled/--disabled',default=False,show_default=True)
  def cli(enabled):pass
  self.assertIn('[default: disabled]',CliRunner().invoke(cli,['--help']).output)

