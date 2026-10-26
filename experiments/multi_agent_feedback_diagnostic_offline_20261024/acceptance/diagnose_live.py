from pathlib import Path
import json
from dotenv import dotenv_values
r=Path.cwd();t=r/'temp/multi_agent_feedback_diagnostic_20261024';rid=json.loads((t/'live_created.json').read_text(encoding='utf-8'))['id'];out=r/'data/task_service/runs'/rid
secrets=[v for k,v in dotenv_values(r/'.env',interpolate=False).items() if 'KEY' in k and v]
summary=json.loads((out/'summary.json').read_text(encoding='utf-8'))
result={'usage':summary['usage'],'reviews':json.loads((out/'reviews.json').read_text(encoding='utf-8')),'responses':json.loads((out/'responses.json').read_text(encoding='utf-8'))}
for name in ('repairer-error.json','revising-trajectory.json'):
    value=json.loads((out/name).read_text(encoding='utf-8'))
    if name.endswith('error.json'): result[name]=value
    else:
        result[name]={'keys':list(value),'last_messages':value.get('messages',[])[-9:]}
from task_workspace import load_task,changed_files
source,manifest=load_task('split-after-boundary');work=Path(summary['run']['workspace'])
result['changed_files']=changed_files(work,manifest)
result['protected_changes']=[p for p,h in manifest['source_files_sha256'].items() if p.startswith('tests/') and __import__('hashlib').sha256((work/p).read_bytes()).hexdigest()!=h]
text=json.dumps(result,ensure_ascii=False,indent=2)
for v in secrets:text=text.replace(v,'[REDACTED]')
(t/'live_diagnosis.json').write_text(text,encoding='utf-8')
print(text[:20000])
