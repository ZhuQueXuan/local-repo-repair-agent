from pathlib import Path
import json
r=Path.cwd();t=r/'temp/multi_agent_response_recheck_20261024';rid=json.loads((t/'live-created.json').read_text(encoding='utf-8'))['id'];run=r/'data/task_service/runs'/rid
summary=json.loads((run/'summary.json').read_text(encoding='utf-8'))
reviews=sorted(json.loads((run/'reviews.json').read_text(encoding='utf-8')),key=lambda v:0 if v['review_id']=='reviewing-review' else 1);responses=json.loads((run/'responses.json').read_text(encoding='utf-8'));roles=summary['roles']
checks=json.loads((run/'response-validation.json').read_text(encoding='utf-8'))
assert all(v['status']=='succeeded' for v in roles)
assert len(reviews)==2 and reviews[0]['verdict']=='request_changes' and reviews[1]['verdict']=='approve'
assert reviews[0]['candidate_id']!=reviews[1]['candidate_id']
assert len(responses)==len(reviews[0]['findings'])==2 and all(v['disposition']=='accepted' and not v['evidence_ids'] for v in responses)
assert all(v['target_candidate_id']==reviews[1]['candidate_id'] and v['reviewed_candidate_id']==reviews[0]['candidate_id'] for v in responses)
assert checks==[checks[0]] and checks[0]['status']=='valid'
phases={v['role_run_id']:v['phase'] for v in roles}
usage=summary['usage']
result={'run_id':rid,'usage':{k:v for k,v in usage.items() if k not in ('by_role','tickets')},'phase_usage':{phases[k]:{a:b for a,b in v.items() if a!='tickets'} for k,v in usage['by_role'].items()},'reviews':[{'review_id':v['review_id'],'candidate_id':v['candidate_id'],'verdict':v['verdict'],'coverage':v['coverage'],'unknowns':v['unknowns'],'findings':v['findings']} for v in reviews],'registered_responses':responses,'metadata_validation':checks,'parent_route':summary['route_state'],'elapsed':usage['total_elapsed_seconds']}
(t/'acceptance-details.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2)[:14000])
