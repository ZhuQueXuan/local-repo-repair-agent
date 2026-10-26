from pathlib import Path
import hashlib,json,sqlite3
r=Path.cwd();t=r/'temp/multi_agent_feedback_diagnostic_20261024'
b=json.loads((t/'before.json').read_text(encoding='utf-8'))
h=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
with sqlite3.connect('file:'+str(r/'data/task_service/runs.sqlite3')+'?mode=ro',uri=True) as db:
    rows=dict(db.execute('select id,payload from runs'))
    pending=[k for k,v in rows.items() if json.loads(v)['status'] in ('queued','running','cancel_requested')]
old=json.loads((t/'production_before_rows.json').read_text(encoding='utf-8'))
facts={'external_model_requests':0,'new_production_runs':len(set(rows)-set(old)),'old_payloads_unchanged':all(rows.get(k)==v for k,v in old.items()),'production_rows':len(rows),'pending':pending,'shared_ledger_unchanged':h(r/'data/deepseek_budget.json')==b['data/deepseek_budget.json'],'normal_policy_unchanged':h(r/'configs/multi_agent_v1.json')==b['configs/multi_agent_v1.json'],'approval':{'action':'Create and start the sole live feedback diagnostic via the production panel','status':'rejected_before_click','reason':'Explicit authorization for source, patch, tests and session data sent to the named model providers, with charges, is required.'}}
assert facts['old_payloads_unchanged'] and facts['shared_ledger_unchanged'] and facts['new_production_runs']==0 and not pending
(t/'actual_facts.json').write_text(json.dumps(facts,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(facts,ensure_ascii=False))
