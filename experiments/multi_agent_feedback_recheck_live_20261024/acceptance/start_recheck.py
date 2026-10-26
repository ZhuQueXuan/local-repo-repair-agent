from pathlib import Path
import json,hashlib,re,uuid,urllib.request,sqlite3
r=Path.cwd();out=r/'temp/multi_agent_feedback_recheck_20261024'
inputs=json.loads((out/'tested-inputs.json').read_text(encoding='utf-8'))
assert all(hashlib.sha256((r/p).read_bytes()).hexdigest()==h for p,h in inputs.items())
with sqlite3.connect('file:'+str(r/'data/task_service/runs.sqlite3')+'?mode=ro',uri=True) as db:
    rows=dict(db.execute('select id,payload from runs'))
assert rows==json.loads((out/'production-before-rows.json').read_text(encoding='utf-8'))
assert not any(json.loads(v)['status'] in ('queued','running','cancel_requested') for v in rows.values())
bodyfile=out/'live-request.json'
if not bodyfile.exists():
    bodyfile.write_text(json.dumps({'task':'split-after-boundary','profile':'adaptive_history','mode':'live','collaboration_mode':'dual_review_diagnostic','policy_version':1,'request_key':uuid.uuid4().hex},indent=2),encoding='utf-8')
body=json.loads(bodyfile.read_text(encoding='utf-8'))
(out/'predeclaration-before-run.md').write_bytes((r/'Project/28_过程_真实反馈诊断与原会话返工验收.md').read_bytes())
base='http://127.0.0.1:8765'
with urllib.request.urlopen(base+'/',timeout=10) as reply: html=reply.read().decode('utf-8')
token=re.search(r"const token='([^']+)'",html).group(1)
request=urllib.request.Request(base+'/api/runs',data=json.dumps(body).encode(),headers={'Content-Type':'application/json','X-Task-Token':token,'Origin':base},method='POST')
with urllib.request.urlopen(request,timeout=15) as reply:created=json.loads(reply.read())
(out/'live-created.json').write_text(json.dumps(created,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:created[k] for k in ('id','kind','task','status','collaboration_mode')},ensure_ascii=False))
