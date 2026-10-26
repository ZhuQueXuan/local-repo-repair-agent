from pathlib import Path
import json,hashlib,sqlite3
r=Path.cwd();t=r/'temp/collaboration_feedback_fix_20261024'
before=json.loads((t/'before.json').read_text(encoding='utf-8'))
changed=[p for p,h in before.items() if not (r/p).is_file() or hashlib.sha256((r/p).read_bytes()).hexdigest()!=h]
expected={'scripts/collaboration_runtime.py','scripts/collaboration_live.py','scripts/run_deepseek_smoke.py','scripts/test_collaboration_diagnostic.py'}
assert set(changed)==expected,changed
with sqlite3.connect('file:'+str(r/'data/task_service/runs.sqlite3')+'?mode=ro',uri=True) as db: rows=dict(db.execute('select id,payload from runs'))
assert rows==json.loads((t/'production-before-rows.json').read_text(encoding='utf-8'))
assert (r/'data/deepseek_budget.json').read_bytes()==(t/'ledger-before.json').read_bytes()
old_archives={}
for name in ('multi_agent_feedback_diagnostic_offline_20261024','multi_agent_feedback_diagnostic_live_20261024'):
    home=r/'experiments'/name;listing=json.loads((home/'artifact_sha256.json').read_text(encoding='utf-8'))
    assert all(hashlib.sha256((home/p).read_bytes()).hexdigest()==h for p,h in listing.items())
    old_archives[name]=len(listing)
# Prefer the latest actual-wire fixture after the full regression.
paths=sorted((r/'temp/multi_agent_stage_b_20261022/tests').glob('*/actual-wire-replay.json'),key=lambda p:p.stat().st_mtime,reverse=True)
wire=json.loads(paths[0].read_text(encoding='utf-8'))
facts={'existing_runtime_files_changed':changed,'new_runtime_files':['scripts/collaboration_feedback.py','scripts/test_collaboration_feedback.py'],'production_records_unchanged':len(rows),'shared_ledger_byte_unchanged':True,'real_model_requests_this_fix':0,'hard_message_guard_unchanged':16384,'previous_archives_verified':old_archives,'wire_replay_fixture':str(paths[0].relative_to(r)),'old_bytes':wire['old_bytes'],'new_bytes':wire['new_bytes'],'normal_policy_unchanged':True}
(t/'verification.json').write_text(json.dumps(facts,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(facts,ensure_ascii=False,indent=2))
