from pathlib import Path
import json,hashlib,sqlite3
r=Path.cwd();t=r/'temp/review_adjudication_20261024'
before=json.loads((t/'before.json').read_text(encoding='utf-8'))
changed=[p for p,h in before.items() if not (r/p).is_file() or hashlib.sha256((r/p).read_bytes()).hexdigest()!=h]
assert not changed,changed
with sqlite3.connect('file:'+str(r/'data/task_service/runs.sqlite3')+'?mode=ro',uri=True) as db:rows=dict(db.execute('select id,payload from runs'))
assert rows==json.loads((t/'production-before-rows.json').read_text(encoding='utf-8'))
assert (r/'data/deepseek_budget.json').read_bytes()==(t/'ledger-before.json').read_bytes()
old={}
for name in ('multi_agent_feedback_diagnostic_offline_20261024','multi_agent_feedback_diagnostic_live_20261024','multi_agent_feedback_fix_20261024'):
    folder=r/'experiments'/name;manifest=json.loads((folder/'artifact_sha256.json').read_text(encoding='utf-8'))
    assert all(hashlib.sha256((folder/p).read_bytes()).hexdigest()==h for p,h in manifest.items())
    old[name]=len(manifest)
facts={'new_implementation_files':['scripts/review_adjudication.py','scripts/adjudication_demo.py','scripts/test_review_adjudication.py'],'changed_existing_runtime_config_data':changed,'unchanged_existing_files':len(before),'unchanged_production_records':len(rows),'shared_ledger_byte_unchanged':True,'new_model_requests':0,'previous_archives_verified':old,'formal_worker_panel_adjudication_integration':False,'runtime_scope':'trusted offline registered probes only, not OS sandbox or natural-language truth judgement'}
(t/'verification.json').write_text(json.dumps(facts,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(facts,ensure_ascii=False,indent=2))
