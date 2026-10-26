from pathlib import Path
import json,hashlib,sqlite3,shutil
r=Path.cwd();t=r/'temp/multi_agent_feedback_recheck_20261024'
previous=json.loads((r/'temp/regression/20261024_200848_4f980c91/protected_before.json').read_text(encoding='utf-8'))
inputs={p:h for p,h in previous.items() if p.startswith(('scripts/','configs/'))}
assert all((r/p).is_file() and hashlib.sha256((r/p).read_bytes()).hexdigest()==h for p,h in inputs.items())
(t/'tested-inputs.json').write_text(json.dumps(inputs,ensure_ascii=False,indent=2),encoding='utf-8')
with sqlite3.connect('file:'+str(r/'data/task_service/runs.sqlite3')+'?mode=ro',uri=True) as db:
    rows=dict(db.execute('select id,payload from runs'))
    assert len(rows)==43 and not any(json.loads(v)['status'] in ('queued','running','cancel_requested') for v in rows.values())
    with sqlite3.connect(t/'production-before.sqlite3') as b:db.backup(b)
(t/'production-before-rows.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
shutil.copy2(r/'data/deepseek_budget.json',t/'ledger-before.json')
source=r/'tasks/split-after-boundary'
task_inputs={str(p.relative_to(r)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in [source/'manifest.json',source/'issue.md',source/'checks/test_behavior.py']}
(t/'precheck.json').write_text(json.dumps({'tested_runtime_inputs':len(inputs),'original_production_rows':43,'pending':0,'task_inputs':task_inputs,'code_changed_this_run':False,'scope':'One new fixed seeded-falsey diagnostic identity after offline fixes. No restart of old identities; no selecting reruns.'},ensure_ascii=False,indent=2),encoding='utf-8')
print('Current code matches',len(inputs),'tested runtime/config files; 43 previous records backed up; no pending runs.')
