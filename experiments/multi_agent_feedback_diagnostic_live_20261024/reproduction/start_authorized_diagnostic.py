from pathlib import Path
import json,re,uuid,urllib.request,sqlite3,hashlib
root=Path.cwd(); out=root/'temp/multi_agent_feedback_diagnostic_20261024'
assert (out/'authorization.json').is_file()
with sqlite3.connect('file:'+str(root/'data/task_service/runs.sqlite3')+'?mode=ro',uri=True) as db:
    rows=dict(db.execute('SELECT id,payload FROM runs'))
    assert not any(json.loads(v)['status'] in ('queued','running','cancel_requested') for v in rows.values())
assert rows==json.loads((out/'production_before_rows.json').read_text(encoding='utf-8'))
inputs=json.loads((out/'tested_inputs.json').read_text(encoding='utf-8'))
assert all(hashlib.sha256((root/p).read_bytes()).hexdigest()==expected for p,expected in inputs.items())
body_path=out/'live_request.json'
if not body_path.exists():
    body_path.write_text(json.dumps({'task':'split-after-boundary','profile':'adaptive_history','mode':'live','collaboration_mode':'dual_review_diagnostic','policy_version':1,'request_key':uuid.uuid4().hex},indent=2),encoding='utf-8')
body=json.loads(body_path.read_text(encoding='utf-8'))
base='http://127.0.0.1:8765'
with urllib.request.urlopen(base+'/',timeout=10) as reply: html=reply.read().decode('utf-8')
token=re.search(r"const token='([^']+)'",html).group(1)
request=urllib.request.Request(base+'/api/runs',data=json.dumps(body).encode(),headers={'Content-Type':'application/json','X-Task-Token':token,'Origin':base},method='POST')
with urllib.request.urlopen(request,timeout=15) as reply: created=json.loads(reply.read())
(out/'live_created.json').write_text(json.dumps(created,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:created[k] for k in ('id','kind','task','status','collaboration_mode')},ensure_ascii=False))
