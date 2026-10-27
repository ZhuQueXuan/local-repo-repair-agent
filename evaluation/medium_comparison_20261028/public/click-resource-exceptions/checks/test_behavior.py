import unittest
import click
from click.testing import CliRunner
class Resource:
 def __init__(self,events,name): self.events,self.name=events,name
 def __enter__(self): return self
 def __exit__(self,typ,value,tb): self.events.append((self.name,typ,value,tb))
class Cases(unittest.TestCase):
 def test_direct_error_reaches_resource(self):
  events=[];error=ValueError('failed')
  with self.assertRaises(ValueError):
   with click.Context(click.Command('c')) as ctx:
    ctx.with_resource(Resource(events,'r'));raise error
  self.assertIs(events[0][1],ValueError);self.assertIs(events[0][2],error);self.assertIsNotNone(events[0][3])
 def test_group_resource_observes_subcommand_failure(self):
  events=[];error=RuntimeError('sub failed')
  @click.group()
  @click.pass_context
  def cli(ctx): ctx.with_resource(Resource(events,'group'))
  @cli.command()
  def sub(): raise error
  result=CliRunner().invoke(cli,['sub'])
  self.assertIs(result.exception,error);self.assertIs(events[0][2],error)
 def test_cleanup_failure_does_not_leave_current_context(self):
  before=click.get_current_context(silent=True)
  class Bad(Resource):
   def __exit__(self,*args): raise LookupError('cleanup')
  with self.assertRaises(LookupError):
   with click.Context(click.Command('c')) as ctx: ctx.with_resource(Bad([],'bad'))
  self.assertIs(click.get_current_context(silent=True),before)
