"""Predeclared original-single versus full-Harness comparison. Default: no models.

Reuses the fixed public packet and existing workers/accounting. Retained checks
run only after ALL model workers end, against clean patch replays. No retry API.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT=Path(__file__).resolve().parent.parent
sys.dont_write_bytecode=True
from comparison_inputs import TASKS,FRESH_TASKS,CURRENT_TASKS,THREE_ARM_TASKS,packet_for,public_task
from collaboration_protocol import canonical_bytes
from evaluation_preparation import copy_public,apply_reference
from task_workspace import prepare_task,changed_files,load_task,digest
from task_service_store import worker_lock,TERMINAL
from collaboration_store import CollaborationStore
from collaboration_panel import public_policy,usage_view

def write(path,value):path.write_bytes(canonical_bytes(value))
def sha(raw):return hashlib.sha256(raw).hexdigest()

DESIGNS={'legacy_six':TASKS,'fresh_two':FRESH_TASKS,'current_two':CURRENT_TASKS,'three_arm_two':THREE_ARM_TASKS}

def design_variants(plan):
    design_tasks(plan)
    return ('baseline','improved','full') if plan.get('design')=='three_arm_two' else ('baseline','full')

def design_tasks(plan):
    name=plan.get('design','legacy_six')
    if name not in DESIGNS:raise ValueError('Unknown comparison design')
    return DESIGNS[name]

def billing_failure(ledger,existing):
    """Only explicit new billing failures stop later slots; never probe balance."""
    for key,row in ledger['requests'].items():
        if key in existing:continue
        failure=row.get('failure',{})
        if failure.get('kind') in {'insufficient_balance','billing_rejected','payment_required'} or failure.get('status_code')==402:
            return {'kind':failure.get('kind'),'provider':failure.get('provider'),'status_code':failure.get('status_code')}
    return None

def batch_root(path):
    path=Path(path).resolve()
    if path.parent!=(ROOT/'temp/comparison_batches').resolve():
        raise ValueError('Batch storage must be a direct child of project temp/comparison_batches')
    return path

def evaluator(task,repo,output):
    """Existing isolated qualification child; outputs never enter a model run."""
    output.mkdir(parents=True,exist_ok=False)
    env={k:v for k,v in os.environ.items() if k.upper() in {'PATH','SYSTEMROOT','WINDIR','COMSPEC','PATHEXT'}}
    env.update(PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1',PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
        TEMP=str(repo/'temp'),TMP=str(repo/'temp'))
    command=[sys.executable,'-B','-I',str(ROOT/'scripts/evaluation_preparation.py'),'--child',str(packet_for(task)),task,str(repo),str(output)]
    with (output/'console.log').open('xb') as log:
        p=subprocess.run(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=240)
    if p.returncode or not (output/'results.json').is_file():
        raise RuntimeError('Independent retained evaluator unavailable; keep slot, do not rerun model')
    return json.loads((output/'results.json').read_bytes())

def qualify(home,tasks=TASKS):
    results={}
    for task in tasks:
        _,manifest=public_task(task)
        packet=packet_for(task)
        reference=json.loads((packet/'evaluator'/task/'reference.json').read_bytes())
        versions={}
        for version in ('baseline','reference'):
            repo=home/'qualification'/task/version/'repo'
            copy_public(packet,task,repo)
            if version=='reference':apply_reference(repo,manifest,reference['edits'])
            versions[version]=evaluator(task,repo,repo.parent/'results')
        for part in ('public','withheld'):
            assert not versions['baseline'][part]['successful'],(task,part,'baseline')
            assert versions['reference'][part]['successful'],(task,part,'reference')
        assert all(v['upstream']['successful'] for v in versions.values()),task
        results[task]=versions
        print(json.dumps({'qualified':task,'external_model_calls':0}),flush=True)
    write(home/'qualification.json',results)
    return results

def input_listing(tasks=TASKS):
    files=set((ROOT/'scripts').glob('*.py'))|set((ROOT/'scripts').glob('*.js'))|set((ROOT/'configs').glob('*.json'))
    files.update((ROOT/'configs').glob('*-requirements.txt'));files.add(ROOT/'scripts/task_panel.html')
    files.update((ROOT/'skills').glob('*/SKILL.md'));files.update((ROOT/'mini-swe-agent/src/minisweagent').rglob('*.py'))
    for task in tasks:
        base,m=public_task(task)
        files.update(base/'repo'/n for n in m['source_files_sha256'])
        files.update(base/n for n in (*m['task_files_sha256'],'manifest.json'))
        files.update((packet_for(task)/'evaluator'/task).glob('*.py'));files.update((packet_for(task)/'evaluator'/task).glob('*.json'))
    return {p.relative_to(ROOT).as_posix():digest(p) for p in sorted(files)}

def prepare(home,design='legacy_six'):
    if design not in DESIGNS:raise ValueError('Unknown comparison design')
    tasks=DESIGNS[design]
    home=batch_root(home);home.mkdir(parents=True,exist_ok=False)
    qualification=qualify(home,tasks)
    slots=[]
    for i,task in enumerate(tasks):
        order=design_variants({'design':design})
        for variant in (order if i%2==0 else tuple(reversed(order))):
            key='comparison-'+uuid.uuid4().hex
            slots.append({'task':task,'variant':variant,'request_key':key,'repetition':1})
    from execution_runtime import runtime_identity
    plan={'schema_version':1,'state':'prepared_not_dispatched','batch_id':home.name,'design':design,'slots':slots,
        'tasks':list(tasks),'variants':list(design_variants({'design':design})),'repetitions':1,
        'baseline':'Original raw-profile DefaultAgent 2.4.6 route through existing worker, 300s; current Windows/accounting adapter, not byte-identical historical deployment.',
        'improved':'Existing adaptive_history single StatefulAgent, 300s, bounded output, history projection and task-state guard. No task card, retrieval, consultation, independent role review, skills or collaboration repair contract.',
        'full':'adaptive_history + fixed document question + human task card + readonly skills/MCP, first repair 300s; original five-phase 1800s parent policy, one possible revision.',
        'confounds':'Prompts, context/state, consultation, review, opportunities and total time differ. Compare entire systems; do not attribute gain to agent count or individual module.',
        'visibility':'Same source/issue/public checks/upstream subset. Retained checks and reference outside all copied workspaces/snapshots/prompts. Local Bash is not an OS sandbox.',
        'scoring':'After all predeclared workers end: latest exported final patch, else last legal sealed candidate for collaboration failures; no candidate = unresolved. Clean replay public+withheld+declared upstream subset. No held-check feedback or selection reruns.',
        'failure_policy':'Every slot remains in denominator, including timeout/cancel/provider/runtime errors. Report errors separately; no dropping or filling with a selected retry.',
        'destinations':['https://myai.bupt.edu.cn/llm-gw/v1','https://api.deepseek.com'],
        'external_content':['public task source','issue','patches','public test results','role conversations','full-arm fixed task card','local skills'],
        'accounting':'Existing shared cumulative budget; no project money cap. Unknown usage/price remains unknown, per-attempt 0.10 reservation is not total cost.',
        'full_question':'Read the frozen README and repository docs relevant to the original issue. Quote exact established compatibility or input constraints; explicitly state missing definitions. Do not infer requirements from unrelated history.',
        'files_sha256':input_listing(tasks),'runtime_identity':runtime_identity(),
        'qualification_sha256':sha((home/'qualification.json').read_bytes()),'paid_launch_authorized':False,
        'fresh_machine':'excluded_by_user_20261027','historical_packets':'Old31/v7 declarations remain unchanged and are never launched by this module.'}
    write(home/'plan.json',plan);write(home/'plan-sha256.json',{'sha256':sha((home/'plan.json').read_bytes())})
    print(json.dumps({'prepared':str(home),'slots':len(slots),'input_files':len(plan['files_sha256']),'external_model_calls':0}),flush=True)
    return plan

def verify(home):
    home=batch_root(home);raw=(home/'plan.json').read_bytes();plan=json.loads(raw)
    if sha(raw)!=json.loads((home/'plan-sha256.json').read_bytes())['sha256']:raise ValueError('Batch plan changed')
    if plan['batch_id']!=home.name:raise ValueError('Batch identity cannot be moved or copied to a new home')
    if plan['schema_version']!=1 or plan['state']!='prepared_not_dispatched' or plan['paid_launch_authorized'] is not False:raise ValueError('Invalid scope-only plan')
    slots=plan['slots']
    tasks=design_tasks(plan);variants=design_variants(plan);expected=len(variants)*len(tasks)
    if plan['tasks']!=list(tasks) or plan['variants']!=list(variants) or plan['repetitions']!=1:raise ValueError('Comparison design changed')
    if len(slots)!=expected or len({s['request_key'] for s in slots})!=expected:raise ValueError('Expected exactly the declared unique slots')
    if any(sorted(s['variant'] for s in slots if s['task']==t)!=sorted(variants) for t in tasks):raise ValueError('Missing comparison arm')
    if any(s['task'] not in tasks or s['repetition']!=1 for s in slots):raise ValueError('Invalid slot')
    for n,h in plan['files_sha256'].items():
        p=(ROOT/n).resolve()
        if not p.is_relative_to(ROOT) or digest(p)!=h:raise ValueError('Frozen input changed: '+n)
    if set(plan['files_sha256'])!=set(input_listing(tasks)):raise ValueError('Frozen input inventory changed')
    from execution_runtime import runtime_identity
    if runtime_identity()!=plan['runtime_identity']:raise ValueError('Runtime environment changed')
    if sha((home/'qualification.json').read_bytes())!=plan['qualification_sha256']:raise ValueError('Qualification changed')
    return plan

def select_patch(store,row):
    run=store.home/'runs'/row['id']
    if (run/'repair.diff').is_file():return run/'repair.diff','exported_final_patch'
    if row['kind']=='collaboration_live' and (run/'summary.json').is_file():
        summary=json.loads((run/'summary.json').read_bytes())
        candidate=store.state(row['id'])['collaboration_state']['candidate_id']
        entry=summary.get('assessments',{}).get(candidate)
        if entry:
            p=Path(entry['patch']).resolve()
            if not p.is_relative_to(run.resolve()):raise ValueError('Patch escaped its run')
            if p.is_file():return p,'last_legal_sealed_candidate'
    return None,'no_sealed_candidate'

def score(home,store,slots):
    # Never evaluate a retained check while ANY model of this batch can run.
    rows=[store.get(s['run_id']) for s in slots]
    if any(r['status'] not in TERMINAL or r['status']=='paused' for r in rows):raise ValueError('All workers must be terminal before scoring')
    results=[]
    for slot,row in zip(slots,rows):
        result={**slot,'status':row['status'],'agent_exit':row.get('agent_exit'),'collaboration_result':row.get('collaboration_result'),
            'repair_result':row['repair_result'],'review_verdict':row.get('review_verdict'),'resolved':False,
            'metrics':row.get('metrics',{}),'reason':row.get('reason')}
        patch,selection=select_patch(store,row);result['candidate_selection']=selection
        if patch is not None:
            try:
                # Recreate trusted public inputs; copy only implementation diff.
                repo,manifest=prepare_task(slot['task'])
                result['patch_sha256']=digest(patch)
                p=subprocess.run(['git','-C',str(repo),'apply','--',str(patch)],capture_output=True,timeout=30)
                if p.returncode:raise ValueError('Final patch cannot replay')
                changes=changed_files(repo,manifest)
                if not changes or any(n not in manifest['source_files_sha256'] or not n.startswith(manifest['source_prefix']) for n in changes):raise ValueError('Patch outside implementation')
                result['checks']=evaluator(slot['task'],repo,home/'terminal-scores'/slot['request_key'])
                result['resolved']=all(result['checks'][k]['successful'] for k in ('public','withheld','upstream'))
            except Exception as error:result['evaluation_error']=type(error).__name__+': '+str(error)[:300]
        else:result['evaluation_error']='No exported or legally sealed final patch; retained in denominator'
        if row['kind']=='collaboration_live':result['usage']=usage_view(store,row)
        results.append(result);write(home/'terminal-results.json',results)
    pairs={t:{r['variant']:r['resolved'] for r in results if r['task']==t} for t in dict.fromkeys(s['task'] for s in slots)}
    report={'slots':len(slots),'results':results,'resolved':{v:sum(r['resolved'] for r in results if r['variant']==v) for v in dict.fromkeys(s['variant'] for s in slots)},
        'pairs':pairs,'scope':'Predeclared public issues, one repetition each. Descriptive system comparison only; no benchmark/population/module attribution.',
        'no_result_selection_reruns':True}
    write(home/'comparison-summary.json',report);return report

def execute(home,authorization):
    home=batch_root(home);plan=verify(home)
    auth=json.loads(Path(authorization).read_bytes())
    if auth!={'approved':True,'plan_sha256':sha((home/'plan.json').read_bytes()),'maximum_runs':len(plan['slots'])}:raise ValueError('Exact batch approval required')
    if (home/'dispatched.json').exists():raise ValueError('Batch already dispatched; no automatic resume or new identity')
    with worker_lock(home,'batch.lock') as acquired:
        if not acquired:raise ValueError('Another batch owner is active')
        with worker_lock(ROOT/'data/task_service') as production_idle:
            if not production_idle:raise ValueError('Formal worker active')
            import sqlite3
            with sqlite3.connect((ROOT/'data/task_service/runs.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
                if any(json.loads(v)['status'] in {'queued','running','cancel_requested','preparing_continuation'} for (v,) in db.execute('SELECT payload FROM runs')):
                    raise ValueError('Formal task active or queued; do not interleave batches')
            store=CollaborationStore(home/'store')
            existing_requests=set(json.loads((ROOT/'data/deepseek_budget.json').read_bytes())['requests'])
            write(home/'dispatched.json',{'plan_sha256':sha((home/'plan.json').read_bytes()),'state':'claiming_once'})
            slots=[]
            # Predeclare every result identity before any model, not only winners.
            for slot in plan['slots']:
                if slot['variant'] in {'baseline','improved'}:
                    row=store.create(slot['task'],'raw' if slot['variant']=='baseline' else 'adaptive_history','live',slot['request_key'],model_profile='legacy_deepseek_v1')
                elif slot['variant']=='full':
                    row=store.create_collaboration(slot['task'],'adaptive_history',slot['request_key'],public_policy(documents=True,question=plan['full_question']),
                        execution='live',model_profile='legacy_deepseek_v1',allow_capabilities=True,task_card={
                        'goal':'Fix the original issue under its existing constraints; program checks and independent review determine outcomes.',
                        'steps':[{'id':'repair','title':'Read, repair and verify the original issue','status':'pending'}],
                        'instruction':'Retain uncertainty and failures. At most one revision; do not loop until success.'})
                else:raise ValueError('Unknown comparison arm')
                slots.append({**slot,'run_id':row['id']})
            write(home/'slots.json',slots)
            for slot in slots:
                verify(home)
                with (home/(slot['request_key']+'.claimed')).open('x') as f:f.write(slot['run_id'])
                command=[sys.executable,'-B',str(ROOT/'scripts/task_service_worker.py'),'--run-id',slot['run_id'],'--home',str(store.home)]
                if slot['variant']=='full':command.append('--collaboration')
                print(json.dumps({'starting':slot},ensure_ascii=False),flush=True)
                with (home/(slot['request_key']+'.log')).open('xb') as log:
                    p=subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                    try:p.wait(timeout=1860)
                    except subprocess.TimeoutExpired:
                        from execution_backend import terminate_owned_process
                        terminate_owned_process(p);p.wait(timeout=10)
                        store.update(slot['run_id'],status='failed',stage='done',reason='Owned worker exceeded batch deadline; do not replay')
                row=store.get(slot['run_id'])
                if row['status'] not in TERMINAL:store.update(slot['run_id'],status='failed',stage='done',reason='Worker exited without terminal record; no automatic retry')
                print(json.dumps({'ended':slot['run_id'],'status':store.get(slot['run_id'])['status']}),flush=True)
                stopped=billing_failure(json.loads((ROOT/'data/deepseek_budget.json').read_bytes()),existing_requests)
                if stopped:
                    write(home/'billing-stop.json',stopped)
                    for pending in slots:
                        if store.get(pending['run_id'])['status']=='queued':store.cancel(pending['run_id'])
                    break
            verify(home)
            return score(home,store,slots)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--home',type=Path,required=True)
    action=p.add_mutually_exclusive_group(required=True);action.add_argument('--prepare',action='store_true');action.add_argument('--verify',action='store_true');action.add_argument('--execute',action='store_true')
    p.add_argument('--authorization',type=Path);p.add_argument('--design',choices=tuple(DESIGNS),default='legacy_six');a=p.parse_args()
    if a.prepare:prepare(a.home,a.design)
    elif a.verify:print(json.dumps({'verified':len(verify(a.home)['slots']),'model_calls':0}))
    else:
        if a.authorization is None:p.error('--execute requires --authorization binding exact plan digest and declared runs')
        report=execute(a.home,a.authorization);print(json.dumps({'resolved':report['resolved'],'slots':report['slots']}))
if __name__=='__main__':main()
