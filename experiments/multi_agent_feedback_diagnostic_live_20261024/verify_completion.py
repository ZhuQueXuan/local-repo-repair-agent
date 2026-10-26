from pathlib import Path
import json,hashlib,zipfile,sqlite3,re
r=Path.cwd();t=r/'temp/multi_agent_feedback_diagnostic_20261024';rid=json.loads((t/'live_created.json').read_text(encoding='utf-8'))['id']
archives={}
for name in ('multi_agent_feedback_diagnostic_offline_20261024','multi_agent_feedback_diagnostic_live_20261024'):
    home=r/'experiments'/name;manifest=json.loads((home/'artifact_sha256.json').read_text(encoding='utf-8'))
    assert all((home/p).is_file() and hashlib.sha256((home/p).read_bytes()).hexdigest()==v for p,v in manifest.items())
    verified_zips={}
    for index in home.glob('*.zip.sha256.json'):
        zpath=Path(str(index)[:-len('.sha256.json')]);entries=json.loads(index.read_text(encoding='utf-8'))
        with zipfile.ZipFile(zpath) as z:
            assert set(z.namelist())==set(entries)
            assert all(hashlib.sha256(z.read(p)).hexdigest()==v for p,v in entries.items())
        verified_zips[zpath.name]=len(entries)
    archives[name]={'sealed_entries':len(manifest),'zip_entries_verified':verified_zips}
inputs=json.loads((r/'data/task_service/runs'/rid/'inputs.json').read_text(encoding='utf-8'))['files_sha256']
assert all(hashlib.sha256((r/p).read_bytes()).hexdigest()==v for p,v in inputs.items())
before=json.loads((t/'ledger_before.json').read_text(encoding='utf-8'));after=json.loads((r/'data/deepseek_budget.json').read_text(encoding='utf-8'))
assert all(after['requests'].get(k)==v for k,v in before['requests'].items())
assert len(set(after['requests'])-set(before['requests']))==19
with sqlite3.connect('file:'+str(r/'data/task_service/runs.sqlite3')+'?mode=ro',uri=True) as db:
    rows=dict(db.execute('select id,payload from runs'))
old=json.loads((t/'production_before_rows.json').read_text(encoding='utf-8'))
assert all(rows.get(k)==v for k,v in old.items()) and set(rows)-set(old)=={rid}
assert not any(json.loads(v)['status'] in ('queued','running','cancel_requested') for v in rows.values())
# Local documentation references must resolve; no additional network requests.
doc=r/'Project/28_过程_真实反馈诊断与原会话返工验收.md'
for link in re.findall(r'\]\(([^)]+)\)',doc.read_text(encoding='utf-8')):
    if not link.startswith(('http','##')):assert (doc.parent/link.split(':')[0]).exists(),link
facts={'run_id':rid,'archives':archives,'frozen_inputs_unchanged':len(inputs),'old_records_unchanged':len(old),'only_one_new_run':True,'only_19_new_tickets':True,'pending_runs':0,'documentation_links_resolve':True,'no_runtime_edits_after_live':True}
(t/'completion_verification.json').write_text(json.dumps(facts,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(facts,ensure_ascii=False,indent=2))
