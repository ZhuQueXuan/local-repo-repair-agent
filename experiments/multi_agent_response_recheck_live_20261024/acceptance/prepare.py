from pathlib import Path
import json,hashlib,sqlite3,shutil,uuid
r=Path.cwd();t=r/'temp/multi_agent_response_recheck_20261024'
previous=json.loads((r/'temp/regression/20261024_203737_bc4b4590/protected_before.json').read_text(encoding='utf-8'))
inputs={p:h for p,h in previous.items() if p.startswith(('scripts/','configs/'))}
assert all(hashlib.sha256((r/p).read_bytes()).hexdigest()==h for p,h in inputs.items())
(t/'tested-inputs.json').write_text(json.dumps(inputs,ensure_ascii=False,indent=2),encoding='utf-8')
with sqlite3.connect('file:'+str(r/'data/task_service/runs.sqlite3')+'?mode=ro',uri=True) as db:
    rows=dict(db.execute('select id,payload from runs'));assert len(rows)==44 and not any(json.loads(v)['status'] in ('queued','running','cancel_requested') for v in rows.values())
    with sqlite3.connect(t/'production-before.sqlite3') as b:db.backup(b)
(t/'production-before-rows.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
shutil.copy2(r/'data/deepseek_budget.json',t/'ledger-before.json')
body={'task':'split-after-boundary','profile':'adaptive_history','mode':'live','collaboration_mode':'dual_review_diagnostic','policy_version':1,'request_key':uuid.uuid4().hex}
(t/'live-request.json').write_text(json.dumps(body,indent=2),encoding='utf-8')
s=(r/'temp/multi_agent_feedback_recheck_20261024/start_recheck.py').read_text(encoding='utf-8-sig').replace('temp/multi_agent_feedback_recheck_20261024','temp/multi_agent_response_recheck_20261024')
s=s.replace("inputs=json.loads((out/'tested-inputs.json')", "assert (out/'authorization.json').is_file(), 'Explicit new identity authorization required'\ninputs=json.loads((out/'tested-inputs.json')")
(t/'start_recheck.py').write_text(s,encoding='utf-8')
print('Prepared the next code-version request and',len(inputs),'tested hashes; no service request or run created.')
