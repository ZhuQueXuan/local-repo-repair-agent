from pathlib import Path
import json
r=Path.cwd();t=r/'temp/review_adjudication_20261024'
info=json.loads((t/'demo-console.log').read_text(encoding='utf-8'));p=Path(info['output']);data=json.loads(p.read_text(encoding='utf-8'))
result={'demo':str(p),'examples':{name:{'summary':v['summary'],'first_decision':{k:v['decisions'][0][k] for k in ('verdict','resolution','impact','improved_checks','regressed_checks')}} for name,v in data['examples'].items()}}
(t/'demo-summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2))
