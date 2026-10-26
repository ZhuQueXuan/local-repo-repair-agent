from pathlib import Path
import json
r=Path.cwd();t=r/'temp/collaboration_feedback_fix_20261024'
new_path=r/'temp/regression/20261024_172323_ca9697bb/adapter/upstream_results.json'
new=json.loads(new_path.read_text(encoding='utf-8')); failures=lambda d:sorted(v['nodeid'] for v in d['reports'] if v['phase']=='call' and v['outcome']=='failed')
old_candidates=sorted((r/'temp/regression').glob('20261023_*/adapter/upstream_results.json'),key=lambda p:p.stat().st_mtime,reverse=True)
old_path=next(p for p in old_candidates if json.loads(p.read_text(encoding='utf-8'))['counts'].get('failed')==6)
old=json.loads(old_path.read_text(encoding='utf-8'))
assert failures(new)==failures(old)
result={'previous':str(old_path.relative_to(r)),'current':str(new_path.relative_to(r)),'same_failed_nodeids':True,'counts':new['counts'],'failures':failures(new),'scope':'3 environment propagation and 3 path spelling differences, retained; default/base hook behavior unchanged.'}
(t/'adapter-comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
