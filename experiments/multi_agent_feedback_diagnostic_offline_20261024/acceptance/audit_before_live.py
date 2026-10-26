from pathlib import Path
import hashlib,json,shutil
r=Path.cwd(); t=r/'temp/multi_agent_feedback_diagnostic_20261024'
before=json.loads((t/'before.json').read_text(encoding='utf-8'))
changed=[p for p,h in before.items() if not (r/p).is_file() or hashlib.sha256((r/p).read_bytes()).hexdigest()!=h]
expected={'scripts/collaboration_live.py','scripts/collaboration_panel.py','scripts/collaboration_store.py','scripts/collaboration_panel.js','scripts/task_panel.html'}
print(json.dumps({'changed_existing':changed,'unexpected':sorted(set(changed)-expected),'shared_ledger_unchanged':'data/deepseek_budget.json' not in changed},ensure_ascii=False))
assert set(changed)==expected
shutil.copy2(r/'data/deepseek_budget.json',t/'ledger_before.json')
(t/'prelive_audit.json').write_text(json.dumps({'changed_existing':changed,'shared_ledger_unchanged':True},indent=2),encoding='utf-8')
