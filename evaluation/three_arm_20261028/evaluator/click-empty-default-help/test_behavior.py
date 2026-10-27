import unittest
import click
from click.testing import CliRunner
def help_for(default,**kwargs):
 @click.command()
 @click.option('--value',default=default,**kwargs)
 def cli(value): pass
 result=CliRunner().invoke(cli,['--help']);assert result.exit_code==0,result.exception
 return result.output
class Cases(unittest.TestCase):
 def test_empty_help_and_runtime_value(self):
  @click.group(context_settings={'show_default':True})
  def cli(): pass
  @cli.command()
  @click.option('--text',default='')
  def sub(text): click.echo(repr(text))
  runner=CliRunner();self.assertIn('[default: ""]',runner.invoke(cli,['sub','--help']).output)
  self.assertEqual(runner.invoke(cli,['sub']).output,"''\n")
 def test_suppressed_custom_and_ordinary_defaults(self):
  self.assertNotIn('default:',help_for('',show_default=False))
  self.assertIn('[default: (empty allowed)]',help_for('',show_default='empty allowed'))
  self.assertIn('[default: hello]',help_for('hello',show_default=True))
  self.assertIn('[default: 0]',help_for(0,show_default=True))
 def test_none_false_flag_and_dynamic_preserved(self):
  self.assertNotIn('default:',help_for(None,show_default=True))
  self.assertNotIn('default:',help_for(False,is_flag=True,show_default=True))
  calls=[]
  def dynamic(): calls.append(True);return ''
  self.assertIn('(dynamic)',help_for(dynamic,show_default=True));self.assertEqual(calls,[])
