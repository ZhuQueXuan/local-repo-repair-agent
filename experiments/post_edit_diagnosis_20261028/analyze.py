"""One bounded read-only audit of stage68; no execution replay or model calls."""
from collections import Counter
import hashlib,json,sqlite3,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];OUT=Path(__file__).parent;HOME=ROOT/'temp/comparison_batches/u68_1028'
def read(p):return json.loads(p.read_bytes())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
ledger=ROOT/'data/deepseek_budget.json';before_ledger=sha(ledger)
with sqlite3.connect((ROOT/'data/task_service/runs.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
 formal=dict(db.execute('SELECT id,payload FROM runs'))
assert len(formal)==53 and len(read(ledger)['requests'])==4415
history={p.relative_to(ROOT).as_posix():sha(p) for d in ('experiments','evaluation') for p in (ROOT/d).rglob('*') if p.is_file()}
source={p.relative_to(ROOT).as_posix():sha(p) for d in ('scripts','configs','mini-swe-agent/src') for p in (ROOT/d).rglob('*') if p.is_file() and '__pycache__' not in p.parts}
records=[]
for slot in read(HOME/'slots.json'):
 run=HOME/'store/runs'/slot['run_id'];trace=run/'trajectory.json';messages=read(trace)['messages']
 edits=[i for i,m in enumerate(messages) if any(a.get('tool')=='repair_edit' for a in m.get('extra',{}).get('actions',[]))]
 assert edits
 first=edits[0];receipt=messages[first+1];assert receipt['role']=='tool'
 later=[a for m in messages[first+2:] for a in m.get('extra',{}).get('actions',[])]
 request_path=run/'task-state/decision-requests.jsonl'
 requests=[json.loads(l) for l in request_path.read_text('utf-8').splitlines()]
 quoted=[r for r in requests if r['http_attempted'] and 'Lists differ:' in r['last_program_message']['content']
  and 'None' in r['last_program_message']['content']]
 full_sources=[{'message_index':i,'tool_call_id':m.get('tool_call_id'),'content':m['content']}
  for i,m in enumerate(messages) if i>first and m['role']=='tool' and '[TRUNCATED:' not in m.get('content','')
  and "cells = [row[i] if i < len(row) else ''" in m.get('content','')]
 records.append({'arm':slot['variant'],'run_id':slot['run_id'],'trajectory_sha256':sha(trace),
  'first_edit_index':first,'first_edit_receipt':receipt['content'],'later_actions':len(later),
  'later_edit_calls':sum(a.get('tool')=='repair_edit' for a in later),
  'actual_request_records_with_expected_vs_actual':len(quoted),
  'last_program_failure_example':quoted[0]['last_program_message'] if quoted else None,
  'untruncated_raw_tool_views_with_remaining_fallback':full_sources[:3],
  'total_untruncated_raw_tool_views_with_remaining_fallback':len(full_sources),
  'source_visibility_scope':'Raw complete tool observations only, not a reconstruction of the entire prepared API context or proof of understanding.',
  'tool_counts':dict(Counter(a.get('tool','bash') for m in messages for a in m.get('extra',{}).get('actions',[])))})
simple=next(r for r in records if r['arm']=='simple')
assert simple['actual_request_records_with_expected_vs_actual']>0 and simple['later_edit_calls']==0
assert simple['total_untruncated_raw_tool_views_with_remaining_fallback']>0
assert sha(ledger)==before_ledger
assert all(sha(ROOT/n)==h for n,h in history.items()) and all(sha(ROOT/n)==h for n,h in source.items())
with sqlite3.connect((ROOT/'data/task_service/runs.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
 assert dict(db.execute('SELECT id,payload FROM runs'))==formal
result={'kind':'Bounded post-edit diagnosis; stopping without adding heuristics',
 'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
 'records':records,'formal_payloads_unchanged':53,'requests_unchanged':4415,
 'historical_files_verified':len(history),'source_and_config_files_unchanged':len(source),
 'new_model_calls':0,'old_commands_executed':0,'new_tests':0,
 'finding':'Concrete failed assertion is present in actually attempted requests. Simple model never edits again. Raw complete relevant views exist, but whole prepared context/understanding is not proven.',
 'unknown':'Cannot infer why the model preferred repeated reads, or separate model capability, context selection and prompt conflict, from this trace.',
 'decision':'Stop root-cause micro-tuning here. Preserve strict default and known-task success; do not lower tests, enlarge the experiment or claim generic stability. Next direction is a reproducible project demonstration and documentation, before any new benchmark or model changes.'}
(OUT/'analysis.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'simple_failure_in_actual_requests':simple['actual_request_records_with_expected_vs_actual'],
 'simple_complete_raw_views':simple['total_untruncated_raw_tool_views_with_remaining_fallback'],
 'simple_later_actions':simple['later_actions'],'simple_later_edits':simple['later_edit_calls'],
 'old_files_unchanged':len(history),'source_unchanged':len(source),'model_calls':0}))
