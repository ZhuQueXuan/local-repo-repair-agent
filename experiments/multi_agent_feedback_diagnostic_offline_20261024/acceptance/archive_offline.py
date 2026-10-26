from pathlib import Path
import json,shutil,hashlib,sqlite3,zipfile
r=Path.cwd();t=r/'temp/multi_agent_feedback_diagnostic_20261024';dest=r/'experiments/multi_agent_feedback_diagnostic_offline_20261024'
dest.mkdir(exist_ok=True)
def copy(source,relative):
    p=dest/relative;p.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,p)
for name in ('scripts/collaboration_diagnostic.py','scripts/test_collaboration_diagnostic.py','scripts/collaboration_live.py','scripts/collaboration_panel.py','scripts/collaboration_store.py','scripts/collaboration_panel.js','scripts/task_panel.html','configs/multi_agent_feedback_diagnostic_v1.json','configs/multi_agent_v1.json'):
    copy(r/name,'implementation/'+name)
for p in t.iterdir():
    if p.is_file() and p.name not in ('panel-server.log','panel-server-resumed.log','live_request.json','live_created.json'):
        copy(p,'acceptance/'+p.name)
reg=r/'temp/regression/20261024_155045_3f4ed638'
for name in ('summary.json','protected_before.json','protected_after.json','project.log','native.log','native/upstream_results.json','native/upstream_junit.xml'):
    copy(reg/name,'regression/'+name)
summaries=[]
for p in (r/'temp/multi_agent_stage_b_20261022/tests').glob('*/runs/*/summary.json'):
    value=json.loads(p.read_text(encoding='utf-8'))
    if value['run'].get('collaboration_mode')=='dual_review_diagnostic':summaries.append((p,value))
selected=[]
for accepted in (True,False):
    choices=[(p,v) for p,v in summaries if (v['run'].get('repair_result')=='passed')==accepted]
    if choices:
        p,v=max(choices,key=lambda pair:pair[0].stat().st_mtime)
        home=p.parents[2]
        with zipfile.ZipFile(dest/('fixture-feedback.zip' if accepted else 'fixture-approve.zip'),'w',zipfile.ZIP_DEFLATED) as archive:
            for f in home.rglob('*'):
                if f.is_file() and f.suffix!='.lock':archive.write(f,f.relative_to(home).as_posix())
        selected.append({'home':str(home.relative_to(r)),'run_id':v['run']['id'],'repair_result':v['run']['repair_result'],'requests_scope':'scripted transport, synthetic ledger only'})
(dest/'README.json').write_text(json.dumps({'scope':'Offline implementation and fixture evidence only; live request is separately authorized and archived.','regression':{'native_passed':301,'project_passed':280,'project_skipped':1},'selected_fixtures':selected,'partial_copy_note':'offline_fixture/ is the preserved incomplete first copy stopped by Windows path length; complete ZIP files are authoritative.'},ensure_ascii=False,indent=2),encoding='utf-8')
from dotenv import dotenv_values
keys=[v for k,v in dotenv_values(r/'.env',interpolate=False).items() if 'KEY' in k and v]
files=[p for p in dest.rglob('*') if p.is_file()]
assert not any(v.encode() in p.read_bytes() for v in keys for p in files)
hashes={p.relative_to(dest).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
(dest/'artifact_sha256.json').write_text(json.dumps(hashes,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'archive':str(dest),'files':len(files),'fixtures':selected,'credential_scan_passed':True},ensure_ascii=False))
