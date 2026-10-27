"""Fixed public comparison projections; evaluator/reference inputs stay outside tasks."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
PACKET=ROOT/'evaluation/multi_agent_20261025'
TASKS=('boltons-addlist-iterator','click-abort-cause','requests-utf8-length',
       'boltons-query-encoding','click-flag-default-map','boltons-unicode-lines')

def public_task(name):
    if name not in TASKS:
        raise ValueError('Not a declared public comparison task')
    from evaluation_preparation import public_manifest
    base,manifest=public_manifest(PACKET,name)
    if not base.resolve().is_relative_to((PACKET/'public').resolve()):
        raise ValueError('Comparison input escaped public projection')
    return base,manifest

def catalog_rows():
    # UI discovery is metadata-only. Every actual use rechecks the public hashes.
    return [{'id':name,'task_id':name,'scope':'Six fixed public comparison issues; no training-contamination or SWE-bench claim. Evaluator checks are terminal-only.',
             'language':'python'} for name in TASKS if (PACKET/'public'/name/'manifest.json').is_file()]
