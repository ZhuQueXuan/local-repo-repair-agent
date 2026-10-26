from pathlib import Path
import json,sys
r=Path.cwd();sys.path.insert(0,str(r/'scripts'));t=r/'temp/multi_agent_feedback_recheck_20261024';rid=json.loads((t/'live-created.json').read_text(encoding='utf-8'))['id'];out=r/'data/task_service/runs'/rid
from collaboration_protocol import FindingResponse,canonical_bytes
from pydantic import ValidationError
summary=json.loads((out/'summary.json').read_text(encoding='utf-8'));work=Path(summary['run']['workspace'])
review=json.loads((out/'reviews.json').read_text(encoding='utf-8'))[0]
roles={v['phase']:v for v in summary['roles']};revision=json.loads((out/'revising-trajectory.json').read_text(encoding='utf-8'))
rows=json.loads((work/'temp/finding_responses.json').read_text(encoding='utf-8'))
reports=json.loads((out/'reports.json').read_text(encoding='utf-8'));target=summary['run']['final_candidate_id']
checks=[]
for row in rows:
    fields={'response_id':'response-'+str(row.get('finding_id')),'run_id':rid,'responder_role_run_id':roles['revising']['role_run_id'],'review_id':review['review_id'],'reviewed_candidate_id':review['candidate_id'],'target_candidate_id':target,**row}
    try:
        FindingResponse.parse(fields);errors=[]
    except ValidationError as error:errors=error.errors(include_input=False,include_url=False)
    checks.append({'finding_id':row.get('finding_id'),'disposition':row.get('disposition'),'reason_type':type(row.get('reason')).__name__,'reason_length':len(row.get('reason','')) if isinstance(row.get('reason'),str) else None,'evidence_ids':row.get('evidence_ids'),'errors':errors})
info={'run_id':rid,'exit_status':revision['info']['exit_status'],'last_exit':revision['messages'][-1], 'review_verdict':review['verdict'],'review_finding_ids':[v['finding_id'] for v in review['findings']],'response_validation':checks,'registered_reports':[{k:v[k] for k in ('report_id','candidate_id','status')} for v in reports],'usage':summary['usage'],'response_count':len(rows),'registered_response_count':len(json.loads((out/'responses.json').read_text(encoding='utf-8')))}
from dotenv import dotenv_values
text=json.dumps(info,ensure_ascii=False,indent=2)
for k,v in dotenv_values(r/'.env',interpolate=False).items():
    if 'KEY' in k and v:text=text.replace(v,'[REDACTED]')
(t/'diagnosis.json').write_text(text,encoding='utf-8');print(text[:12000])
