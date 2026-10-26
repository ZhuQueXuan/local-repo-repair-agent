from pathlib import Path
import json,hashlib,zipfile
from dotenv import dotenv_values
r=Path.cwd();t=r/'temp/review_adjudication_20261024';dest=r/'experiments/review_adjudication_20261024';dest.mkdir(exist_ok=False)
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
for name in ('scripts/review_adjudication.py','scripts/adjudication_demo.py','scripts/test_review_adjudication.py'):
    copy(r/name,'implementation/'+name)
for p in t.iterdir():
    if p.is_file():copy(p,'acceptance/'+p.name)
for label,path in (('default','20261024_200848_4f980c91'),('adapter','20261024_200852_45fa13d2')):
    reg=r/'temp/regression'/path
    for name in ('summary.json','protected_before.json','protected_after.json','project.log','native.log','native/upstream_results.json','native/upstream_junit.xml','adapter.log','adapter/upstream_results.json','adapter/upstream_junit.xml'):
        if (reg/name).is_file():copy(reg/name,'regression/'+label+'/'+name)
demo=Path(json.loads((t/'demo-console.log').read_text(encoding='utf-8'))['output'])
copy(demo,'demonstration.json')
fixture_entries=zipfiles([(p,p.relative_to(demo.parent).as_posix()) for p in demo.parent.rglob('*') if p.is_file()],'full-demonstration.zip')
docs=(r/'Project/29_过程_审查意见独立裁决与有害返工检查.md',r/'Project/00_项目现状与快速接手.md',r/'Project/项目开发流水账.md',r/'README.md',r/'AGENTS.md',r.parent/'job/AGENTS.md')
zipfiles([(p,'job/AGENTS.md' if p.parent==r.parent/'job' else 'shixi_project/'+p.relative_to(r).as_posix()) for p in docs],'documentation.zip')
meta={'scope':'Trusted offline adjudication kernel and artificial demonstration. Not integrated into production worker/panel, no general natural-language judgement or model effectiveness claim.', 'targeted_tests_passed':20,'upstream_native_passed':301,'project_tests_run':310,'project_tests_passed':309,'project_tests_skipped':1,'adapter':{'passed':169,'unchanged_failures':6,'skipped':1},'real_model_calls':0,'formal_new_runs':0,'complete_demonstration_entries':fixture_entries}
(dest/'README.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
manifest={p.relative_to(dest).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in dest.rglob('*') if p.is_file()}
(dest/'artifact_sha256.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
assert all(hashlib.sha256((dest/p).read_bytes()).hexdigest()==h for p,h in manifest.items())
print(json.dumps({'archive':str(dest),'sealed_files':len(manifest),'complete_fixture_entries':fixture_entries,'credential_scan_passed':True},ensure_ascii=False))
