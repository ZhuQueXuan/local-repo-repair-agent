from pathlib import Path
import json,hashlib,shutil
r=Path.cwd();t=r/'temp/multi_agent_feedback_diagnostic_20261024'
f=json.loads((r/'temp/regression/20261024_155045_3f4ed638/protected_before.json').read_text(encoding='utf-8'))
paths={p:v for p,v in f.items() if p.startswith(('scripts/','configs/'))}
mismatches=[p for p,v in paths.items() if not (r/p).is_file() or hashlib.sha256((r/p).read_bytes()).hexdigest()!=v]
print('regression source/config count',len(paths),'mismatches',mismatches)
assert not mismatches
assert 'scripts/collaboration_diagnostic.py' in paths and 'configs/multi_agent_feedback_diagnostic_v1.json' in paths
(t/'tested_inputs.json').write_text(json.dumps(paths,ensure_ascii=False,indent=2),encoding='utf-8')
(t/'authorization.json').write_text(json.dumps({'date':'2026-10-24','human_confirmation':'确认这一次诊断及上述外发、费用范围','scope':'One live seeded_falsey_remainder_v1 diagnostic via panel; public source/patch/test results/role session to existing BUPT endpoint, existing DeepSeek official fallback; shared accounting, parent 1800 seconds, at most one revision; no reruns or selection.'},ensure_ascii=False,indent=2),encoding='utf-8')
