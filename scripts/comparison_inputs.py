"""Fixed public comparison projections; evaluator/reference inputs stay outside tasks."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
PACKET=ROOT/'evaluation/multi_agent_20261025'
TASKS=('boltons-addlist-iterator','click-abort-cause','requests-utf8-length',
       'boltons-query-encoding','click-flag-default-map','boltons-unicode-lines')
FRESH_TASKS=('boltons-indexed-update','boltons-aware-relative')
FRESH_PACKET=ROOT/'evaluation/fresh_comparison_20261027'
CURRENT_TASKS=('boltons-empty-record-names','boltons-bits-width')
CURRENT_PACKET=ROOT/'evaluation/current_comparison_20261027'

def packet_for(name):
    if name in TASKS:return PACKET
    if name in FRESH_TASKS:return FRESH_PACKET
    if name in CURRENT_TASKS:return CURRENT_PACKET
    raise ValueError('Not a declared public comparison task')

def public_task(name):
    packet=packet_for(name)
    from evaluation_preparation import public_manifest
    base,manifest=public_manifest(packet,name)
    if not base.resolve().is_relative_to((packet/'public').resolve()):
        raise ValueError('Comparison input escaped public projection')
    return base,manifest

def catalog_rows():
    # UI discovery is metadata-only. Every actual use rechecks the public hashes.
    return [{'id':name,'task_id':name,'scope':'Six fixed public comparison issues; no training-contamination or SWE-bench claim. Evaluator checks are terminal-only.',
             'language':'python'} for name in (*TASKS,*FRESH_TASKS,*CURRENT_TASKS) if (packet_for(name)/'public'/name/'manifest.json').is_file()]
