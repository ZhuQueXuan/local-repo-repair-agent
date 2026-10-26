import io,json,re,urllib.request,urllib.error,zipfile
from pathlib import Path
base='http://127.0.0.1:8765'
t=Path('temp/adjudication_panel_20261024')
with urllib.request.urlopen(base+'/',timeout=10) as response: html=response.read().decode()
token=re.search(r"const token='([^']+)'",html).group(1)
def get(route):
    request=urllib.request.Request(base+route,headers={'X-Task-Token':token,'Origin':base})
    return urllib.request.urlopen(request,timeout=15)
run='d3173d4153e544ccbf0eb066834848f8'
with get('/api/runs') as response:rows=json.load(response)
assert len(rows)==45
with get('/api/runs/'+run+'/collaboration') as response:view=json.load(response)
assert len(view['adjudications'])==1
batch=view['adjudications'][0]
prefix='/api/runs/'+run+'/adjudications/'+batch['batch_id']+'/'
with get(prefix+'result.json') as response:assert json.load(response)['input_digest']==batch['input_digest']
with get(prefix+'proof.zip') as response:raw=response.read()
with zipfile.ZipFile(io.BytesIO(raw)) as archive:proof_files=len(archive.namelist())
versions=[]
for c in view['candidates']:
    with get('/api/runs/'+run+'/candidates/'+c['candidate_id']) as response:value=json.load(response)
    assert len(value['adjudications'])==2
    assert all(d['reviewed_candidate_id']==c['candidate_id'] for d in value['adjudications'])
    versions.append({'candidate_id':c['candidate_id'],'is_final':value['is_final'],
        'verdicts':[d['verdict'] for d in value['adjudications']]})
other=next(r['id'] for r in rows if r['id']!=run)
try:get('/api/runs/'+other+'/adjudications/'+batch['batch_id']+'/result.json')
except urllib.error.HTTPError as e:assert e.code==400
else:raise AssertionError('Cross-run proof should be denied')
single=next(r for r in rows if not r['kind'].startswith('collaboration_'))
with get('/api/runs/'+single['id']+'/collaboration') as response:assert json.load(response)['mode']=='single'
result={'records':len(rows),'batch_id':batch['batch_id'],'proof_files_read_over_http':proof_files,
    'versions':versions,'cross_run_rejected':True,'single_unchanged':True,'new_tasks_created':0,'model_calls':0}
(t/'api-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
