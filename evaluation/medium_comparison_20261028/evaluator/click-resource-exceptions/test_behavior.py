import unittest
from contextlib import contextmanager
import click
from click.testing import CliRunner
class Cases(unittest.TestCase):
 def test_nested_same_context_defers_and_forwards_error(self):
  seen=[];ctx=click.Context(click.Command('c'));error=KeyError('x')
  @contextmanager
  def managed():
   try: yield
   except BaseException as e: seen.append(e);raise
  with self.assertRaises(KeyError):
   with ctx:
    ctx.with_resource(managed())
    with ctx: pass
    self.assertEqual(seen,[]);raise error
  self.assertEqual(seen,[error])
 def test_lifo_callbacks_success_and_explicit_close(self):
  seen=[];ctx=click.Context(click.Command('c'))
  class R:
   def __enter__(self): return self
   def __exit__(self,typ,value,tb): seen.append(('resource',typ,value,tb))
  ctx.with_resource(R());ctx.call_on_close(lambda:seen.append('callback'));ctx.close()
  self.assertEqual(seen,['callback',('resource',None,None,None)]);ctx.close();self.assertEqual(len(seen),2)
 def test_function_resource_and_context_reuse_after_failure(self):
  seen=[];ctx=click.Context(click.Command('c'))
  @contextmanager
  def managed():
   try: yield
   except ValueError: seen.append('rollback');raise
   else: seen.append('commit')
  with self.assertRaises(ValueError):
   with ctx: ctx.with_resource(managed());raise ValueError('x')
  with ctx: ctx.with_resource(managed())
  self.assertEqual(seen,['rollback','commit'])
 def test_nested_command_resources_have_same_error_and_reverse_order(self):
  seen=[];error=OSError('x')
  class R:
   def __init__(self,name): self.name=name
   def __enter__(self): return self
   def __exit__(self,typ,value,tb): seen.append((self.name,typ,value,tb is not None))
  @click.group()
  @click.pass_context
  def cli(ctx): ctx.with_resource(R('group'))
  @cli.command()
  @click.pass_context
  def sub(ctx): ctx.with_resource(R('sub'));raise error
  result=CliRunner().invoke(cli,['sub']);self.assertIs(result.exception,error)
  self.assertEqual(seen,[('sub',OSError,error,True),('group',OSError,error,True)])
 def test_stack_is_clean_and_consumed_after_cleanup_raises(self):
  before=click.get_current_context(silent=True);seen=[];ctx=click.Context(click.Command('c'))
  def bad(): seen.append('bad');raise RuntimeError('cleanup')
  with self.assertRaises(RuntimeError):
   with ctx: ctx.call_on_close(bad)
  self.assertIs(click.get_current_context(silent=True),before)
  with ctx: ctx.call_on_close(lambda:seen.append('new'))
  self.assertEqual(seen,['bad','new'])
