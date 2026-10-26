from pathlib import Path
import json,uuid,hashlib
r=Path.cwd();t=r/'temp/multi_agent_feedback_recheck_20261024'
path=t/'live-request.json'
if not path.exists():path.write_text(json.dumps({'task':'split-after-boundary','profile':'adaptive_history','mode':'live','collaboration_mode':'dual_review_diagnostic','policy_version':1,'request_key':uuid.uuid4().hex},indent=2),encoding='utf-8')
(t/'predeclaration-before-run.md').write_bytes((r/'Project/28_过程_真实反馈诊断与原会话返工验收.md').read_bytes())
(t/'approval-block.json').write_text(json.dumps({'action':'Start exactly one new live feedback recheck via the existing task-service API','status':'rejected_before_process_creation','new_run_created':False,'new_model_request_sent':False,'reason':'Automatic review requires explicit human confirmation of this new identity, public source/patch/tests/session data, BUPT and DeepSeek destinations and fees.'},ensure_ascii=False,indent=2),encoding='utf-8')
print('Prepared one persistent request key and saved the predeclaration/rejection; no live API called.')
