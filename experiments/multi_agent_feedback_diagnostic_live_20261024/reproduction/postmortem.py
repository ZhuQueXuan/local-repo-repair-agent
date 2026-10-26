from pathlib import Path
import json,sys,sqlite3,hashlib,difflib,shutil,subprocess
r=Path.cwd();sys.path.insert(0,str(r/'scripts'))
from task_workspace import prepare_task,load_task,digest
from task_service_worker import collect_patch
from collaboration_diagnostic import SOURCE
from collaboration_protocol import canonical_bytes
out=r/'temp/multi_agent_feedback_diagnostic_20261024';rid=json.loads((out/'live_created.json').read_text(encoding='utf-8'))['id'];actual=r/'data/task_service/runs'/rid
summary=json.loads((actual/'summary.json').read_text(encoding='utf-8'));work=Path(summary['run']['workspace']);source,manifest=load_task('split-after-boundary')
target=out/'postmortem';target.mkdir(exist_ok=False)
# Reproduce collection rejection without deleting cache or editing the actual run.
try:
    collect_patch('split-after-boundary',work,target,manifest)
except RuntimeError as error:
    collection_error=str(error)
else:raise AssertionError('Expected original collection rejection')
assert '.pytest_cache' in collection_error
trajectory=json.loads((actual/'revising-trajectory.json').read_text(encoding='utf-8'))
messages=trajectory['messages'];receipts=[m for m in messages if m.get('extra',{}).get('completed_submission_receipt')]
initial=json.loads((actual/'repairing-trajectory.json').read_text(encoding='utf-8'))['messages']
assert messages[1]['content']==initial[1]['content'] and len(receipts)==1
roles=summary['roles'];repair=next(p for p in roles if p['phase']=='repairing');revision=next(p for p in roles if p['phase']=='revising')
assert repair['session_id']==revision['session_id']
raw=(work/SOURCE).read_bytes();raw_hash=digest(work/SOURCE)
clean,_=prepare_task('split-after-boundary');(clean/SOURCE).write_bytes(raw)
changed=collect_patch('split-after-boundary',clean,target,manifest)
checks={}
def evaluate(repo,label):
    folder=target/label;folder.mkdir()
    report=folder/'report.json'
    completed=subprocess.run([sys.executable,'-B','scripts/check_task.py','--task','split-after-boundary','--repo',str(repo),'--output',str(report),'--full-suite'],cwd=r,capture_output=True,timeout=240)
    (folder/'console.log').write_bytes(completed.stdout+completed.stderr)
    standard=json.loads(report.read_text(encoding='utf-8'))
    diag=folder/'diagnostic.json'
    completed=subprocess.run([sys.executable,'-B','scripts/collaboration_diagnostic.py','--repo',str(repo),'--output',str(diag)],cwd=r,capture_output=True,timeout=30)
    (folder/'diagnostic-console.log').write_bytes(completed.stdout+completed.stderr)
    extra=json.loads(diag.read_text(encoding='utf-8'))
    return {'issue':standard['issue_checks']['passed'],'targeted':standard['upstream_targeted']['passed'],'full_passed':standard['full_upstream_suite']['passed'],'full_run':standard['full_upstream_suite']['tests_run'],'full_failures':standard['full_upstream_suite']['failures'],'diagnostic_passed':extra['passed'],'diagnostic_run':extra['tests_run'],'protected_tests_unchanged':standard['protected_tests_unchanged']}
checks['unregistered_revised_source']=evaluate(clean,'revised-source')
replay,_=prepare_task('split-after-boundary')
subprocess.run(['git','-C',str(replay),'apply','--',str(target/'repair.diff')],check=True,capture_output=True)
checks['manual_extraction_patch_replay']=evaluate(replay,'replay')
assert digest(work/SOURCE)==raw_hash
responses=work/'temp/finding_responses.json'
response_payload=json.loads(responses.read_text(encoding='utf-8')) if responses.is_file() else None
result={'run_id':rid,'scope':'Postmortem offline extraction and grading only; does not register a candidate or change live failure, no model calls.',
    'collection_error':collection_error,'agent_exit':trajectory.get('info',{}),'last_exit':messages[-1],
    'same_original_task':True,'same_repair_session':True,'completed_submission_receipts':len(receipts),'initial_submission_tool_id':receipts[0].get('tool_call_id'),
    'revised_source_sha256':raw_hash,'changed_implementation':changed,'checks':checks,'unregistered_response_file':response_payload}
(target/'result.json').write_bytes(canonical_bytes(result))
print(json.dumps(result,ensure_ascii=False,indent=2)[:7500])
