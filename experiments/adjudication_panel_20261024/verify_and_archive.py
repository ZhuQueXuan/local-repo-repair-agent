from pathlib import Path
import hashlib, json, sqlite3, sys, zipfile, shutil
root = Path.cwd()
scratch = root / 'temp/adjudication_panel_20261024'
sys.path.insert(0, str(root / 'scripts'))
from collaboration_protocol import canonical_bytes, sha256
from collaboration_store import CollaborationStore
from collaboration_adjudication import read_batches, read_proof
run_id = 'd3173d4153e544ccbf0eb066834848f8'
home = root / 'data/task_service'
before = json.loads((scratch / 'before.json').read_bytes())
old_rows = json.loads((scratch / 'production-before-rows.json').read_bytes())
with sqlite3.connect('file:' + str(home / 'runs.sqlite3') + '?mode=ro', uri=True) as db:
    rows = dict(db.execute('SELECT id,payload FROM runs'))
    assert rows == old_rows, 'Original payload changed'
    assert not any(json.loads(v)['status'] in ('queued','running','cancel_requested') for v in rows.values())
    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert db.execute('SELECT count(*) FROM collaboration_adjudications').fetchone()[0] == 1
    with sqlite3.connect(scratch / 'production-after.sqlite3') as target:
        db.backup(target)
assert (root / 'data/deepseek_budget.json').read_bytes() == (scratch / 'ledger-before.json').read_bytes()
changed = [p for p, h in before.items() if sha256((root / p).read_bytes()) != h]
expected = {'scripts/collaboration_store.py','scripts/collaboration_panel.py','scripts/collaboration_panel.js',
            'scripts/collaboration_live.py','scripts/task_service.py','scripts/task_service_worker.py','data/task_service/runs.sqlite3'}
assert set(changed) == expected, changed
store = CollaborationStore(home, allow_production=True)
batches = read_batches(store, run_id)
assert len(batches) == 1
batch = batches[0]
assert sum(d['verdict']=='confirmed' for d in batch['decisions']) == 1
assert sum(d['verdict']=='unresolved' for d in batch['decisions']) == 3
with zipfile.ZipFile(__import__('io').BytesIO(read_proof(store, run_id, batch['batch_id'], 'proof.zip'))) as archive:
    hashes = json.loads(read_proof(store, run_id, batch['batch_id'], 'proof-files.json'))
    assert set(hashes) == set(archive.namelist())
    assert all(sha256(archive.read(p)) == h for p,h in hashes.items())
result = {'scope':'Supplemental retrospective adjudication only; no model calls or score rewriting.',
    'original_payloads_identical':len(rows),'ledger_identical':True,'new_model_requests':0,
    'new_adjudication_batches':len(batches),'batch_id':batch['batch_id'],
    'changed_existing_files':changed,'unchanged_existing_files':len(before)-len(changed),
    'proof_files_verified':len(hashes),'confirmed_findings':1,'unresolved_findings':3,
    'candidate_pass_counts':sorted(sum(v=='passed' for v in r['results'].values()) for r in batch['receipts']),
    'new_discovery_count':0, 'snapshot_archive_label_note':'Older snapshot-v1/v2 ZIP names follow archive iteration, not semantic chronology; candidate IDs/hashes define lineage.'}
(scratch / 'verification.json').write_bytes(canonical_bytes(result))
archive = root / 'experiments/adjudication_panel_20261024'
archive.mkdir(exist_ok=True)
for name in ('before.json','production-before.sqlite3','production-before-rows.json','production-after.sqlite3','ledger-before.json',
             'verification.json','targeted.log','targeted-elevated.log','regression.log','verify_and_archive.py','api-verification.json',
             'browser-verification.json','ui-check.json','adapter-comparison.json','targeted-final.log','formal-adjudication.log',
             'verify_api.py','rendered-proof.json','panel-proof.png','initial-candidate-ax.txt','panel.log','panel-error.log','adapter.log'):
    if (scratch / name).is_file(): shutil.copy2(scratch / name,archive / name)
shutil.copy2(root / 'data/deepseek_budget.json',archive / 'ledger-after.json')
shutil.copytree(home / 'adjudications' / batch['batch_id'], archive / 'registered-batch', dirs_exist_ok=True)
def pack(name, paths):
    with zipfile.ZipFile(archive / name,'w',zipfile.ZIP_DEFLATED) as bundle:
        for path in paths:
            if path.is_file(): bundle.write(path,path.relative_to(root).as_posix())
sources = [root/p for p in sorted(expected) if p.startswith('scripts/')]
sources += [root/'scripts'/n for n in ('collaboration_adjudication.py','adjudication_checks.py','test_collaboration_adjudication.py','review_adjudication.py','review_tools.py','collaboration_protocol.py')]
sources += [root/'configs/adjudication_bindings_v1.json']
pack('implementation.zip',sources)
regression = root / 'temp/regression/20261024_211557_58be62c0'
pack('regression.zip',regression.rglob('*'))
adapter = scratch / 'adapter-output-path.json'
if adapter.exists(): pack('adapter.zip',Path(json.loads(adapter.read_bytes())['path']).rglob('*'))
docs = [root/p for p in ('AGENTS.md','README.md','Project/00_项目现状与快速接手.md','Project/29_过程_审查意见独立裁决与有害返工检查.md','Project/项目开发流水账.md')]
pack('documentation.zip',docs)
with zipfile.ZipFile(archive/'documentation.zip','a',zipfile.ZIP_DEFLATED) as bundle:
    bundle.write(root.parent/'job/AGENTS.md','job/AGENTS.md')
backup_paths = sorted(home.glob('before_adjudication_*.sqlite3'))
assert len(backup_paths)==1
shutil.copy2(backup_paths[0],archive/'automatic-pre-migration.sqlite3')
(archive/'README.json').write_bytes(canonical_bytes({'scope':result['scope'], 'run_id':run_id,'registered_batch':batch['batch_id'],
    'proof':'registered-batch/proof.zip contains frozen probe, mappings, two full candidate check copies, receipts and decisions.',
    'production_change':'One additive table and one appended batch; all 45 original payloads and ledger identical.',
    'limits':'Only six disclosed cases. Exactly one retrospective trusted finding binding; other claims unresolved. No arbitrary-language adjudication, new-task comparison or OS sandbox.',
    'snapshot_archive_note':result['snapshot_archive_label_note']}))
from dotenv import dotenv_values
secrets=[v.encode() for k,v in dotenv_values(root/'.env',interpolate=False).items() if 'KEY' in k and v]
for path in archive.rglob('*'):
    if not path.is_file(): continue
    assert not any(secret in path.read_bytes() for secret in secrets), 'Credential in archive'
    if path.suffix=='.zip':
        with zipfile.ZipFile(path) as bundle:
            for name in bundle.namelist():
                assert not any(secret in bundle.read(name) for secret in secrets), 'Credential in ZIP member'
manifest={p.relative_to(archive).as_posix():sha256(p.read_bytes()) for p in archive.rglob('*') if p.is_file() and p!=archive/'artifact_sha256.json'}
(archive/'artifact_sha256.json').write_bytes(canonical_bytes(manifest))
assert all(sha256((archive/p).read_bytes())==h for p,h in manifest.items())
print(json.dumps(result | {'archived_files':len(manifest)},ensure_ascii=False))
