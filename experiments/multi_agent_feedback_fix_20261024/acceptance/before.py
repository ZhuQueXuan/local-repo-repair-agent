from pathlib import Path
import json,hashlib,sqlite3,shutil
r=Path.cwd();t=r/'temp/collaboration_feedback_fix_20261024'
files=[p for name in ('scripts','configs','data') for p in (r/name).rglob('*') if p.is_file() and p.suffix!='.lock' and not any(part in ('runs','snapshots','sealing') for part in p.relative_to(r).parts)]
listing={p.relative_to(r).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
(t/'before.json').write_text(json.dumps(listing,ensure_ascii=False,indent=2),encoding='utf-8')
with sqlite3.connect('file:'+str(r/'data/task_service/runs.sqlite3')+'?mode=ro',uri=True) as db:
    rows=dict(db.execute('select id,payload from runs'))
    assert not any(json.loads(v)['status'] in ('queued','running','cancel_requested') for v in rows.values())
    with sqlite3.connect(t/'production-before.sqlite3') as b:db.backup(b)
(t/'production-before-rows.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
shutil.copy2(r/'data/deepseek_budget.json',t/'ledger-before.json')
print('Baseline saved;',len(rows),'existing runs; no pending runs.')
