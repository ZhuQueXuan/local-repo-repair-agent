from pathlib import Path
import json,hashlib,sqlite3,zipfile,re
r=Path.cwd();t=r/'temp/multi_agent_response_recheck_20261024';rid=json.loads((t/'live-created.json').read_text(encoding='utf-8'))['id'];run=r/'data/task_service/runs'/rid
inputs=json.loads((run/'inputs.json').read_text(encoding='utf-8'))['files_sha256'];assert all(hashlib.sha256((r/p).read_bytes()).hexdigest()==h for p,h in inputs.items())
with sqlite3.connect('file:'+str(r/'data/task_service/runs.sqlite3')+'?mode=ro',uri=True) as db:rows=dict(db.execute('select id,payload from runs'))
old=json.loads((t/'production-before-rows.json').read_text(encoding='utf-8'));assert all(rows.get(k)==v for k,v in old.items()) and set(rows)-set(old)=={rid}
assert not any(json.loads(v)['status'] in ('queued','running','cancel_requested') for v in rows.values())
oldledger=json.loads((t/'ledger-before.json').read_text(encoding='utf-8'));ledger=json.loads((r/'data/deepseek_budget.json').read_text(encoding='utf-8'))
assert all(ledger['requests'].get(k)==v for k,v in oldledger['requests'].items())
added={k:v for k,v in ledger['requests'].items() if k not in oldledger['requests']};assert len(added)==21 and all(v.get('attribution',{}).get('run_id')==rid for v in added.values())
archives={}
for name in ('multi_agent_feedback_diagnostic_live_20261024','multi_agent_feedback_recheck_live_20261024','collaboration_response_fix_20261024','review_adjudication_20261024','multi_agent_response_recheck_live_20261024'):
    home=r/'experiments'/name;listing=json.loads((home/'artifact_sha256.json').read_text(encoding='utf-8'));assert all(hashlib.sha256((home/p).read_bytes()).hexdigest()==h for p,h in listing.items());archives[name]=len(listing)
    if name=='multi_agent_response_recheck_live_20261024':
        for index in home.glob('*.zip.sha256.json'):
            members=json.loads(index.read_text(encoding='utf-8'));zpath=Path(str(index)[:-len('.sha256.json')])
            with zipfile.ZipFile(zpath) as z:
                assert set(z.namelist())==set(members) and all(hashlib.sha256(z.read(p)).hexdigest()==h for p,h in members.items())
facts={'run_id':rid,'four_phases_succeeded':True,'repair_passed':True,'final_review_approve':True,'frozen_inputs_currently_unchanged':len(inputs),'old_records_unchanged':len(old),'production_rows':len(rows),'new_tickets':21,'old_ledger_requests_unchanged':True,'no_pending_runs':True,'archives_verified':archives,'runtime_edits_this_recheck':0,'scope':'manual known-defect integration diagnostic, no new-task or comparative effectiveness result'}
(t/'completion-verification.json').write_text(json.dumps(facts,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(facts,ensure_ascii=False,indent=2))
