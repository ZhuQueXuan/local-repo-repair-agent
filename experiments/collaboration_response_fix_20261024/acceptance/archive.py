from pathlib import Path
import json,hashlib,zipfile
from dotenv import dotenv_values
r=Path.cwd();t=r/'temp/collaboration_response_fix_20261024';dest=r/'experiments/collaboration_response_fix_20261024';dest.mkdir(exist_ok=False)
keys=[v.encode() for k,v in dotenv_values(r/'.env',interpolate=False).items() if 'KEY' in k and v]
def safe(raw):assert not any(v in raw for v in keys),'Credential scan failed'
def copy(p,name):
    raw=p.read_bytes();safe(raw);f=dest/name;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(raw)
def zipfiles(entries,name):
    hashes={}
    with zipfile.ZipFile(dest/name,'w',zipfile.ZIP_DEFLATED) as z:
        for p,relative in entries:
            raw=p.read_bytes();safe(raw);z.writestr(relative,raw);hashes[relative]=hashlib.sha256(raw).hexdigest()
    (dest/(name+'.sha256.json')).write_text(json.dumps(hashes,ensure_ascii=False,indent=2),encoding='utf-8')
    return len(hashes)
for name in ('scripts/collaboration_responses.py','scripts/test_collaboration_responses.py','scripts/collaboration_live.py','scripts/collaboration_feedback.py','scripts/test_collaboration_diagnostic.py','configs/deepseek_budget.json','configs/multi_agent_feedback_diagnostic_v1.json'):
    copy(r/name,'implementation/'+name)
for p in t.iterdir():
    if p.is_file():copy(p,'acceptance/'+p.name)
reg=r/'temp/regression/20261024_203737_bc4b4590'
for name in ('summary.json','protected_before.json','protected_after.json','project.log','native.log','native/upstream_results.json','native/upstream_junit.xml'):
    copy(reg/name,'regression/'+name)
choices={True:[],False:[]}
for p in (r/'temp/multi_agent_stage_b_20261022/tests').glob('*/runs/*/response-validation.json'):
    checks=json.loads(p.read_text(encoding='utf-8'));statuses=[v['status'] for v in checks]
    key=True if statuses==['rejected','valid'] else False if statuses==['rejected']*3 else None
    if key is None:continue
    frozen=json.loads((p.parent/'inputs.json').read_text(encoding='utf-8'))['files_sha256']
    if all((r/k).is_file() and hashlib.sha256((r/k).read_bytes()).hexdigest()==h for k,h in frozen.items()):choices[key].append(p)
fixtures={}
for good in (True,False):
    p=max(choices[good],key=lambda v:v.stat().st_mtime);home=p.parents[2]
    name='corrected-full-chain.zip' if good else 'persistent-invalid-stopped.zip'
    count=zipfiles([(f,f.relative_to(home).as_posix()) for f in home.rglob('*') if f.is_file() and f.suffix!='.lock'],name)
    fixtures[name]={'original_home':str(home.relative_to(r)),'files':count,'scope':'Scripted model only, synthetic ledger, actual files/processes.'}
docs=(r/'Project/28_过程_真实反馈诊断与原会话返工验收.md',r/'Project/00_项目现状与快速接手.md',r/'Project/项目开发流水账.md',r/'README.md',r/'AGENTS.md',r.parent/'job/AGENTS.md')
zipfiles([(p,'job/AGENTS.md' if p.parent==r.parent/'job' else 'shixi_project/'+p.relative_to(r).as_posix()) for p in docs],'documentation.zip')
meta={'scope':'Offline response metadata correction after one real recheck failure; no extra real calls and no old result override.','parser_tests_passed':6,'diagnostic_workflow_tests_passed':10,'native_passed':301,'project_passed':317,'project_skipped':1,'fixtures':fixtures,'real_recheck_archive':'../multi_agent_feedback_recheck_live_20261024'}
(dest/'README.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
manifest={p.relative_to(dest).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in dest.rglob('*') if p.is_file()}
(dest/'artifact_sha256.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8');assert all(hashlib.sha256((dest/p).read_bytes()).hexdigest()==h for p,h in manifest.items())
print(json.dumps({'archive':str(dest),'sealed_files':len(manifest),'fixtures':fixtures,'credential_scan_passed':True},ensure_ascii=False))
