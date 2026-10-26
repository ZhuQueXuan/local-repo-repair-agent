from pathlib import Path
import json,hashlib,zipfile,shutil
from dotenv import dotenv_values
r=Path.cwd();t=r/'temp/collaboration_feedback_fix_20261024';dest=r/'experiments/multi_agent_feedback_fix_20261024';dest.mkdir(exist_ok=False)
keys=[v.encode() for k,v in dotenv_values(r/'.env',interpolate=False).items() if 'KEY' in k and v]
def safe(raw):assert not any(k in raw for k in keys),'Credential scan failed'
def copy(p,name):
    raw=p.read_bytes();safe(raw);f=dest/name;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(raw)
def zipfiles(entries,name):
    hashes={}
    with zipfile.ZipFile(dest/name,'w',zipfile.ZIP_DEFLATED) as z:
        for p,relative in entries:
            raw=p.read_bytes();safe(raw);z.writestr(relative,raw);hashes[relative]=hashlib.sha256(raw).hexdigest()
    (dest/(name+'.sha256.json')).write_text(json.dumps(hashes,ensure_ascii=False,indent=2),encoding='utf-8')
    return len(hashes)
for name in ('scripts/collaboration_feedback.py','scripts/test_collaboration_feedback.py','scripts/collaboration_runtime.py','scripts/collaboration_live.py','scripts/run_deepseek_smoke.py','scripts/test_collaboration_diagnostic.py','configs/deepseek_budget.json','configs/multi_agent_v1.json','configs/multi_agent_feedback_diagnostic_v1.json'):
    copy(r/name,'implementation/'+name)
for p in t.iterdir():
    if p.is_file():copy(p,'acceptance/'+p.name)
for label,path in (('final','20261024_172638_62602dfc'),('intermediate','20261024_172326_975a250b'),('adapter','20261024_172323_ca9697bb')):
    reg=r/'temp/regression'/path
    for name in ('summary.json','protected_before.json','protected_after.json','project.log','native.log','native/upstream_results.json','native/upstream_junit.xml','adapter.log','adapter/upstream_results.json','adapter/upstream_junit.xml'):
        if (reg/name).is_file():copy(reg/name,'regression/'+label+'/'+name)
facts=json.loads((t/'verification.json').read_text(encoding='utf-8'));wire=r/facts['wire_replay_fixture'];copy(wire,'actual-wire-replay.json')
entries=[(p,p.relative_to(wire.parent).as_posix()) for p in wire.parent.rglob('*') if p.is_file() and p.suffix!='.lock']
wire_entries=zipfiles(entries,'wire-replay-fixture.zip')
# Full diagnostic fixture from final code, including source/input hashes and both snapshots.
choices=[]
for p in (r/'temp/multi_agent_stage_b_20261022/tests').glob('*/runs/*/summary.json'):
    d=json.loads(p.read_text(encoding='utf-8'))
    if d['run'].get('collaboration_mode')=='dual_review_diagnostic' and d['run'].get('repair_result')=='passed':
        frozen=json.loads((p.parent/'inputs.json').read_text(encoding='utf-8'))['files_sha256']
        if all((r/k).is_file() and hashlib.sha256((r/k).read_bytes()).hexdigest()==h for k,h in frozen.items()):choices.append((p,d))
p,d=max(choices,key=lambda pair:pair[0].stat().st_mtime);home=p.parents[2]
feedback_entries=zipfiles([(f,f.relative_to(home).as_posix()) for f in home.rglob('*') if f.is_file() and f.suffix!='.lock'],'full-feedback-fixture.zip')
docs=(r/'Project/28_过程_真实反馈诊断与原会话返工验收.md',r/'Project/00_项目现状与快速接手.md',r/'Project/项目开发流水账.md',r/'README.md',r/'AGENTS.md',r.parent/'job/AGENTS.md')
zipfiles([(p,'job/AGENTS.md' if p.parent==r.parent/'job' else 'shixi_project/'+p.relative_to(r).as_posix()) for p in docs],'documentation.zip')
metadata={'scope':'Offline fixes only; no real-model rerun or change to the failed live run.', 'regression':{'native_passed':301,'project_run':290,'project_passed':289,'project_skipped':1,'adapter_passed':169,'adapter_existing_failures':6,'adapter_skipped':1},'wire_bytes':{'before':16807,'after':13154,'hard_guard':16384},'wire_fixture_entries':wire_entries,'full_feedback_fixture_entries':feedback_entries,'feedback_fixture':{'original_home':str(home.relative_to(r)),'run_id':d['run']['id'],'scope':'scripted response/real local processes, synthetic ledger only'},'python_scope':'Current catalog/evaluation/symbol retrieval are Python-specific; no cross-language integration claim.'}
(dest/'README.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
manifest={f.relative_to(dest).as_posix():hashlib.sha256(f.read_bytes()).hexdigest() for f in dest.rglob('*') if f.is_file()}
(dest/'artifact_sha256.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
assert all(hashlib.sha256((dest/f).read_bytes()).hexdigest()==h for f,h in manifest.items())
print(json.dumps({'archive':str(dest),'sealed_entries':len(manifest),'wire_fixture_entries':wire_entries,'feedback_fixture_entries':feedback_entries,'credential_scan_passed':True},ensure_ascii=False))
