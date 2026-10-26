from pathlib import Path
import json,hashlib,sqlite3
r=Path.cwd();t=r/'temp/multi_agent_response_recheck_20261024'
inputs=json.loads((t/'tested-inputs.json').read_text(encoding='utf-8'))
assert all((r/p).is_file() and hashlib.sha256((r/p).read_bytes()).hexdigest()==h for p,h in inputs.items())
with sqlite3.connect('file:'+str(r/'data/task_service/runs.sqlite3')+'?mode=ro',uri=True) as db:
    rows=dict(db.execute('select id,payload from runs'))
assert rows==json.loads((t/'production-before-rows.json').read_text(encoding='utf-8'))
assert (r/'data/deepseek_budget.json').read_bytes()==(t/'ledger-before.json').read_bytes()
assert not (t/'live-created.json').exists()
(t/'authorization.json').write_text(json.dumps({'date':'2026-10-24','human_confirmation':'确认这一新版复验及上述外发、费用范围','scope':'Exactly one new response-correction diagnostic identity, fixed public source/patch/tests/role session to BUPT gateway and existing DeepSeek official fallback; shared accounting, parent 1800 seconds, one revision, up to three metadata submissions within original phase deadline; preserve old failures, no selecting reruns.'},ensure_ascii=False,indent=2),encoding='utf-8')
print('Verified',len(inputs),'tested code/config hashes; original 44 records and ledger unchanged. Authorization saved.')
