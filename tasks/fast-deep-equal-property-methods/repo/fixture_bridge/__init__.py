"""Trusted task-only bridge. The implementation under repair is JavaScript."""
import json,shutil,subprocess
from pathlib import Path
REPO=Path(__file__).resolve().parents[1]
NODE=shutil.which('node')
if not NODE:raise RuntimeError('This task requires the declared Node.js runtime')
probe=subprocess.run([NODE,'-e',"console.log(require.resolve('./lib/equal.js'))"],cwd=REPO,capture_output=True,text=True,check=True,timeout=10)
if Path(probe.stdout.strip()).resolve()!=(REPO/'lib/equal.js').resolve():raise RuntimeError('JavaScript import escaped task')
PROGRAM=r"""
const fs=require('node:fs');
const equal=require('./lib/equal.js');
function decode(v){
 if(Array.isArray(v))return v.map(decode);
 if(v!==null&&typeof v==='object'){
  if(v.__fixture_kind__==='null-prototype')return Object.assign(Object.create(null),decode(v.values));
  if(v.__fixture_kind__==='date')return new Date(v.value);
  if(v.__fixture_kind__==='number-object')return new Number(v.value);
  if(v.__fixture_kind__==='nan')return NaN;
  if(v.__fixture_kind__==='regexp')return new RegExp(v.source,v.flags);
  return Object.fromEntries(Object.entries(v).map(([k,x])=>[k,decode(x)]));
 }
 return v;
}
const p=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(equal(decode(p.left),decode(p.right))));
"""
def evaluate(left,right):
    result=subprocess.run([NODE,'-e',PROGRAM],cwd=REPO,input=json.dumps({'left':left,'right':right}),capture_output=True,text=True,timeout=10)
    if result.returncode:raise RuntimeError('JavaScript implementation failed: '+result.stderr[-1800:])
    value=json.loads(result.stdout)
    if type(value) is not bool:raise RuntimeError('Comparator did not return a boolean')
    return value
