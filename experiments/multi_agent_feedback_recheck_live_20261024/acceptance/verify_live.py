from pathlib import Path
import json,sqlite3,hashlib,re,urllib.request
r=Path.cwd();t=r/'temp/multi_agent_feedback_recheck_20261024';rid=json.loads((t/'live-created.json').read_text(encoding='utf-8'))['id'];home=r/'data/task_service';run=home/'runs'/rid
before=json.loads((t/'ledger-before.json').read_text(encoding='utf-8'));ledger=json.loads((r/'data/deepseek_budget.json').read_text(encoding='utf-8'))
assert all(ledger['requests'].get(k)==v for k,v in before['requests'].items())
added={k:v for k,v in ledger['requests'].items() if k not in before['requests']}
assert len(added)==21 and all(v.get('attribution',{}).get('run_id')==rid for v in added.values())
with sqlite3.connect('file:'+str(home/'runs.sqlite3')+'?mode=ro',uri=True) as db:rows=dict(db.execute('select id,payload from runs'))
old=json.loads((t/'production-before-rows.json').read_text(encoding='utf-8'));assert all(rows.get(k)==v for k,v in old.items()) and set(rows)-set(old)=={rid}
record=json.loads(rows[rid]);summary=json.loads((run/'summary.json').read_text(encoding='utf-8'));work=Path(record['workspace'])
roles={v['phase']:v for v in summary['roles']}
initial=json.loads((run/'repairing-trajectory.json').read_text(encoding='utf-8'));revision=json.loads((run/'revising-trajectory.json').read_text(encoding='utf-8'))
assert revision['messages'][:len(initial['messages'])]==initial['messages']
assert roles['repairing']['session_id']==roles['revising']['session_id']
assert revision['info']['exit_status']=='Submitted'
receipts=[v for v in revision['messages'] if v.get('extra',{}).get('completed_submission_receipt')];assert len(receipts)==1
inputs=json.loads((run/'inputs.json').read_text(encoding='utf-8'))['files_sha256'];assert all(hashlib.sha256((r/p).read_bytes()).hexdigest()==h for p,h in inputs.items())
base='http://127.0.0.1:8765'
with urllib.request.urlopen(base+'/',timeout=10) as reply:page=reply.read().decode()
token=re.search(r"const token='([^']+)'",page).group(1);projection=t/'service-projections';projection.mkdir(exist_ok=True)
def read(path,name):
    request=urllib.request.Request(base+path,headers={'X-Task-Token':token})
    with urllib.request.urlopen(request,timeout=10) as reply:value=json.loads(reply.read())
    (projection/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');return value
index=read('/api/runs/'+rid+'/collaboration','collaboration.json');assert len(index['candidates'])==2 and not index['responses']
for i,c in enumerate(index['candidates']):
    candidate=read('/api/runs/'+rid+'/candidates/'+c['candidate_id'],'candidate-v'+str(i+1)+'.json')
    if i==0:assert 'if rest and any(rest):' in candidate['diff']
    else:assert 'if rest:' in candidate['diff'] and 'if rest and any(rest):' not in candidate['diff']
for i,e in enumerate(index['evidences']):read('/api/runs/'+rid+'/evidence/'+e['evidence_id'],'evidence-'+str(i+1)+'.json')
checks={}
for name in ('repairing-verification','revising-verification','patch-replay'):
    p=json.loads((run/name/'report.json').read_text(encoding='utf-8'));checks[name]={'issue':p['issue_checks']['passed'],'targeted':p['upstream_targeted']['passed'],'diagnostic':p['diagnostic_checks']['passed'],'full_passed':p['full_upstream_suite']['passed'],'full_run':p['full_upstream_suite']['tests_run'],'remaining_failures':p['full_upstream_suite']['failures'],'protected_tests_unchanged':p['protected_tests_unchanged']}
providers={}
for v in added.values():providers[v['provider']]=providers.get(v['provider'],0)+1
facts={'run_id':rid,'status':record['status'],'repair_result':record['repair_result'],'review_verdict':record['review_verdict'],'collaboration_result':record['collaboration_result'],'roles':{k:v['status'] for k,v in roles.items()},'same_original_history_and_session':True,'completed_initial_submit_receipt':len(receipts),'revision_exit':'Submitted','registered_candidates':2,'registered_responses':0,'checks':checks,'shared_old_requests_unchanged':True,'new_tickets':21,'providers':providers,'old_payloads_unchanged':len(old),'production_rows':len(rows),'frozen_input_count':len(inputs),'all_frozen_inputs_currently_unchanged':True,'root_pytest_cache_exists':(work/'.pytest_cache').exists(),'usage':record['metrics'],'scope':'Repair passed and replayed, but collaboration failed at response metadata validation; final review did not run. Not effectiveness evidence.'}
(t/'verified-facts.json').write_text(json.dumps(facts,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in facts.items() if k!='usage'},ensure_ascii=False,indent=2))
