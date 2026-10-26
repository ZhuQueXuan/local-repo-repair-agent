from pathlib import Path
import json,sqlite3,zipfile,hashlib
from dotenv import dotenv_values
r=Path.cwd();t=r/'temp/multi_agent_response_recheck_20261024';rid=json.loads((t/'live-created.json').read_text(encoding='utf-8'))['id'];home=r/'data/task_service';run=home/'runs'/rid;dest=r/'experiments/multi_agent_response_recheck_live_20261024';dest.mkdir(exist_ok=False)
keys=[v.encode() for k,v in dotenv_values(r/'.env',interpolate=False).items() if 'KEY' in k and v]
def safe(raw):assert not any(k in raw for k in keys),'Credential scan failed'
def copy(p,name):
    raw=p.read_bytes();safe(raw);f=dest/name;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(raw)
def zip_tree(source,name):
    hashes={}
    with zipfile.ZipFile(dest/name,'w',zipfile.ZIP_DEFLATED) as z:
        for p in source.rglob('*'):
            if p.is_file() and p.suffix!='.lock':
                raw=p.read_bytes();safe(raw);relative=p.relative_to(source).as_posix();z.writestr(relative,raw);hashes[relative]=hashlib.sha256(raw).hexdigest()
    (dest/(name+'.sha256.json')).write_text(json.dumps(hashes,ensure_ascii=False,indent=2),encoding='utf-8')
    return len(hashes)
original=json.loads((run/'artifact_sha256.json').read_text(encoding='utf-8'));assert all(hashlib.sha256((run/p).read_bytes()).hexdigest()==h for p,h in original.items())
run_entries=zip_tree(run,'run.zip')
for name in ('summary.json','run_config.json','inputs.json','manual-seed.json','reports.json','reviews.json','responses.json','handoffs.json','evidences.json','repair.diff','repairing-trajectory.json','revising-trajectory.json','reviewing-trajectory.json','final_review-trajectory.json','repair-context-projections.json','response-validation.json'):
    copy(run/name,'run-readable/'+name)
with sqlite3.connect('file:'+str(home/'runs.sqlite3')+'?mode=ro',uri=True) as db:
    with sqlite3.connect(dest/'production-after.sqlite3') as b:db.backup(b)
    record=json.loads(db.execute('select payload from runs where id=?',(rid,)).fetchone()[0]);snapshots=[json.loads(v[0]) for v in db.execute("select payload from collaboration_objects where run_id=? and kind='snapshot'",(rid,))]
(dest/'final-record.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
for i,s in enumerate(snapshots):zip_tree(home/s['relative_directory'],'snapshot-v'+str(i+1)+'.zip')
work=Path(record['workspace']);zip_tree(work,'actual-workspace.zip');copy(work/'temp/finding_responses.json','accepted-finding-responses.json')
zip_tree(t/'service-projections','service-projections.zip')
for p in t.iterdir():
    if p.is_file():copy(p,'acceptance/'+p.name)
copy(r/'data/deepseek_budget.json','ledger-after.json');copy(r/'temp/task_service'/rid/'worker.log','worker.log')
meta={'run_id':rid,'scope':'One authorized new-code-version manual-seed diagnostic, all four phases completed; normal revision Submitted, valid metadata, independent final approve and clean replay. Not new-task effectiveness evaluation.','original_run_entries_verified':len(original),'run_zip_entries':run_entries,'registered_candidates':len(snapshots),'fee_scope':'Known estimate 0.177842 CNY plus unknown 0.10 reservation; complete fees unknown.','browser_scope':'Existing task-service API/Supervisor/worker; final browser clicking and user operation not claimed.','metadata_scope':'Accepted twice with empty evidence IDs, first submission valid. Bounded correction branch was not exercised by real model this run; offline branch evidence retained.'}
(dest/'README.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
manifest={p.relative_to(dest).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in dest.rglob('*') if p.is_file()}
(dest/'artifact_sha256.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8');assert all(hashlib.sha256((dest/p).read_bytes()).hexdigest()==h for p,h in manifest.items())
print(json.dumps({'archive':str(dest),'sealed_files':len(manifest),'original_run_entries_verified':len(original),'run_zip_entries':run_entries,'credential_scan_passed':True},ensure_ascii=False))
