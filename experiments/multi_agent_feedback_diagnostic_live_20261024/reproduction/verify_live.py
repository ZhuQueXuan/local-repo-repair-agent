from pathlib import Path
import json,hashlib,sqlite3,re,urllib.request,sys
r=Path.cwd();t=r/'temp/multi_agent_feedback_diagnostic_20261024';rid=json.loads((t/'live_created.json').read_text(encoding='utf-8'))['id'];home=r/'data/task_service'
ledger=json.loads((r/'data/deepseek_budget.json').read_text(encoding='utf-8'));before=json.loads((t/'ledger_before.json').read_text(encoding='utf-8'))
assert all(ledger['requests'].get(k)==v for k,v in before['requests'].items())
added={k:v for k,v in ledger['requests'].items() if k not in before['requests']}
assert len(added)==19 and all(v.get('attribution',{}).get('run_id')==rid for v in added.values())
with sqlite3.connect('file:'+str(home/'runs.sqlite3')+'?mode=ro',uri=True) as db:
    rows=dict(db.execute('select id,payload from runs'))
old=json.loads((t/'production_before_rows.json').read_text(encoding='utf-8'))
assert all(rows.get(k)==v for k,v in old.items()) and set(rows)-set(old)=={rid}
record=json.loads(rows[rid]);assert record['status']=='failed'
inputs=json.loads((r/'data/task_service/runs'/rid/'inputs.json').read_text(encoding='utf-8'))['files_sha256']
assert all(hashlib.sha256((r/p).read_bytes()).hexdigest()==h for p,h in inputs.items())
# Read projections through the same protected HTTP routes consumed by the page.
base='http://127.0.0.1:8765'
with urllib.request.urlopen(base+'/',timeout=10) as reply:html=reply.read().decode('utf-8')
token=re.search(r"const token='([^']+)'",html).group(1)
projection=t/'service-projections';projection.mkdir(exist_ok=False)
def read(route,name):
    req=urllib.request.Request(base+route,headers={'X-Task-Token':token})
    with urllib.request.urlopen(req,timeout=10) as reply:value=json.loads(reply.read())
    (projection/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    return value
index=read('/api/runs/'+rid+'/collaboration','collaboration.json')
assert len(index['candidates'])==1 and not index['responses']
c=index['candidates'][0]
candidate=read('/api/runs/'+rid+'/candidates/'+c['candidate_id'],'candidate-v1.json')
assert 'if rest and any(rest):' in candidate['diff']
for i,e in enumerate(index['evidences']):read('/api/runs/'+rid+'/evidence/'+e['evidence_id'],'evidence-'+str(i+1)+'.json')
providers={}
for v in added.values():
    p=v['provider'];providers[p]=providers.get(p,0)+1
facts={'run_id':rid,'run_status':record['status'],'repair_result':record['repair_result'],'review_verdict':record['review_verdict'],'production_rows':len(rows),'old_payloads_unchanged':True,'new_production_runs':1,'old_ledger_requests_unchanged':True,'new_tickets':len(added),'providers':providers,'frozen_inputs_unchanged':True,'frozen_file_count':len(inputs),'only_old_seed_candidate_registered':True,'registered_responses':0,'http_projection_and_evidence_verified':True,'browser_click_and_result_view':'not verified this run; Work browser control disabled','usage':record['metrics']}
(t/'live_verified_facts.json').write_text(json.dumps(facts,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in facts.items() if k!='usage'},ensure_ascii=False))
