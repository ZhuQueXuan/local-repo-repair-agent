from pathlib import Path
import json,hashlib,sqlite3
r=Path.cwd();t=r/'temp/collaboration_response_fix_20261024';before=json.loads((t/'before.json').read_text(encoding='utf-8'))
changed=[p for p,h in before.items() if not (r/p).is_file() or hashlib.sha256((r/p).read_bytes()).hexdigest()!=h]
expected={'scripts/collaboration_live.py','scripts/collaboration_feedback.py','scripts/test_collaboration_diagnostic.py'}
assert set(changed)==expected,changed
with sqlite3.connect('file:'+str(r/'data/task_service/runs.sqlite3')+'?mode=ro',uri=True) as db:rows=dict(db.execute('select id,payload from runs'))
assert rows==json.loads((t/'production-before-rows.json').read_text(encoding='utf-8'))
assert (r/'data/deepseek_budget.json').read_bytes()==(t/'ledger-before.json').read_bytes()
old={}
for name in ('multi_agent_feedback_recheck_live_20261024','multi_agent_feedback_diagnostic_live_20261024','multi_agent_feedback_fix_20261024','review_adjudication_20261024'):
    home=r/'experiments'/name;listing=json.loads((home/'artifact_sha256.json').read_text(encoding='utf-8'));assert all(hashlib.sha256((home/p).read_bytes()).hexdigest()==h for p,h in listing.items());old[name]=len(listing)
result={'changed_existing':changed,'new_files':['scripts/collaboration_responses.py','scripts/test_collaboration_responses.py'],'post_recheck_original_rows_unchanged':len(rows),'post_recheck_ledger_byte_unchanged':True,'model_calls_in_offline_fix':0,'old_archives_unchanged':old}
(t/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False))
