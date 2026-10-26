from pathlib import Path
import json
r=Path.cwd();t=r/'temp/review_adjudication_20261024'
paths=list((r/'temp/regression').glob('20261024_*/adapter/upstream_results.json'))
path=max(paths,key=lambda p:p.stat().st_mtime)
new=json.loads(path.read_text(encoding='utf-8'));oldpath=r/'temp/regression/20261024_172323_ca9697bb/adapter/upstream_results.json';old=json.loads(oldpath.read_text(encoding='utf-8'))
failed=lambda d:sorted(v['nodeid'] for v in d['reports'] if v['phase']=='call' and v['outcome']=='failed')
assert new['counts']==old['counts'] and failed(new)==failed(old)
value={'current':str(path.relative_to(r)),'previous':str(oldpath.relative_to(r)),'counts':new['counts'],'failed_nodeids':failed(new),'same_failures':True}
(t/'adapter-comparison.json').write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(value,ensure_ascii=False))
