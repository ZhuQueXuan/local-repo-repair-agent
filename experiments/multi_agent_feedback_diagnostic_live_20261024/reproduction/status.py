from pathlib import Path
import json,sqlite3
r=Path.cwd();t=r/'temp/multi_agent_feedback_diagnostic_20261024';rid=json.loads((t/'live_created.json').read_text(encoding='utf-8'))['id']
with sqlite3.connect('file:'+str(r/'data/task_service/runs.sqlite3')+'?mode=ro',uri=True) as db:
    row=json.loads(db.execute('select payload from runs where id=?',(rid,)).fetchone()[0])
    roles=[json.loads(v[0]) for v in db.execute('select payload from role_runs where run_id=? order by rowid',(rid,))]
print(json.dumps({'run':{k:row.get(k) for k in ('id','status','stage','reason','repair_result','review_verdict')},'roles':[{k:v.get(k) for k in ('phase','status','reason')} for v in roles]},ensure_ascii=False))
output=r/'data/task_service/runs'/rid
print('artifacts',sorted(p.name for p in output.iterdir()) if output.exists() else [])
