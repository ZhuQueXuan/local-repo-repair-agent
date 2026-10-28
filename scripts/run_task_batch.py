"""One original-Agent run per task, with frozen evaluation and shared accounting."""
import argparse
from datetime import datetime
import difflib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

sys.dont_write_bytecode = True
from run_deepseek_smoke import BudgetedDeepSeekModel, GitBashEnvironment
from dotenv import dotenv_values
from minisweagent.agents.default import DefaultAgent
from task_workspace import ROOT, changed_files, digest, load_task, prepare_task, package_root
from tool_output_budget import ToolOutputBudget
from context_budget import ContextBudget
from task_state import TaskState
from state_agent import StatefulAgent, NoProgress
from evaluation_freeze import verify_freeze
from repository_retrieval import RepositoryRetrieval
from execution_runtime import bash_executable
from edit_decision import DecisionRejected, parse_command, PREFIXES


class TaskEnvironment(GitBashEnvironment):
    def shell_command(self, command):
        return "set -o pipefail\n" + command

    def execute(self, action, cwd="", *, timeout=None):
        return super().execute(action | {"command": self.shell_command(action["command"])}, cwd, timeout=timeout)


class BoundedTaskEnvironment(TaskEnvironment):
    def __init__(self, work, bash, *, max_output_bytes, python_import_root=None):
        super().__init__(work, bash, python_import_root=python_import_root)
        self.output_budget = ToolOutputBudget(Path(work), max_output_bytes=max_output_bytes)

    def _process_output(self, output, *, command):
        return self.output_budget.process(output, command=command)

    def serialize(self):
        data = super().serialize()
        data["info"]["tool_output_budget"] = self.output_budget.statistics()
        return data


class RecordedModel(BudgetedDeepSeekModel):
    def __init__(self, *args, repository_retrieval=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.request_measurements = []
        self.repository_retrieval = repository_retrieval

    def _query(self, messages, **kwargs):
        measurement = {"next_request": self.requests + 1,
                       "message_utf8_bytes": len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))}
        self.request_measurements.append(measurement)
        before = self.requests
        try:
            response = super()._query(messages, **kwargs)
            if self.repository_retrieval is not None:
                self.repository_retrieval.acknowledge_delivery()
            return response
        finally:
            measurement["http_attempted"] = self.requests > before
            state = getattr(self, 'task_state', None)
            if state is not None and getattr(state, 'decision', None) is not None and messages:
                last = messages[-1]
                if last.get('role') == 'user' and '"program_edit_decision_v1":true' in last.get('content',''):
                    with (state.directory/'decision-requests.jsonl').open('a',encoding='utf-8') as stream:
                        stream.write(json.dumps(measurement|{'last_program_message':last},ensure_ascii=False)+'\n')

    def _calculate_cost(self, response):
        result = super()._calculate_cost(response)
        self.usage[-1]["response_model"] = response.model
        return result


class ContextManagedModel(RecordedModel):
    def __init__(self, *args, context_policy, **kwargs):
        super().__init__(*args, **kwargs)
        if context_policy["target_bytes"] >= self.budget_config["max_messages_utf8_bytes"]:
            raise ValueError("Context target must leave room below the hard request guard.")
        self.context_budget = ContextBudget(**context_policy)

    def _prepare_messages_for_api(self, messages):
        return self.project_context(messages, RecordedModel._prepare_messages_for_api(self, messages))

    def project_context(self, messages, prepared, pinned=None):
        pinned = list(pinned or [])
        if self.repository_retrieval is not None:
            pinned.append(self.repository_retrieval.pinned_message())
        reserved = len(json.dumps(pinned, ensure_ascii=False).encode("utf-8")) + 2 if pinned else 0
        projected = self.context_budget.project(messages, prepared, reserved_bytes=reserved)
        result = projected[:2] + pinned + projected[2:]
        # A large completed edit can exceed the hard guard even with only four
        # recent exchanges. Archive whole completed pairs, never split tool IDs
        # or shorten the original task. The task-card mixin appends its card.
        from context_budget import message_bytes
        receipt = getattr(self, '_task_card_receipt', None)
        card = [receipt['message']] if receipt else []
        hard = getattr(self, 'budget_config', {}).get('max_messages_utf8_bytes', 16384)
        if message_bytes(result + card) > hard:
            policy = self.context_budget
            for keep in (1, 0):
                emergency = ContextBudget(target_bytes=policy.target_bytes, keep_recent_tool_outputs=max(1, keep),
                    compact_completed_exchanges=True, retained_audit_entries=policy.audit_entries, compact_to_target=True)
                emergency.work, emergency.history_directory = policy.work, policy.history_directory
                # Zero is used only here, after the ordinary projection fails.
                reserved = message_bytes(pinned + card) + 2 if pinned or card else 0
                compact, replaced = emergency._compact_completed(messages, [dict(m) for m in prepared],
                    max(512, policy.target_bytes-reserved), keep_recent=keep)
                result = compact[:2] + pinned + compact[2:]
                policy.events.append({'mode': 'hard_guard_completed_projection', 'keep_recent_tool_outputs': keep,
                    'after_bytes': message_bytes(result + card), 'hard_limit': hard,
                    'task_card_reserved': bool(card), 'replaced_outputs': replaced, 'raw_history_preserved': True})
                if message_bytes(result + card) <= hard:
                    break
        return result


class StatefulTaskEnvironment(BoundedTaskEnvironment):
    def _check_finished(self,output):
        if self.task_state.decision is not None:
            from execution_progress import checkout_body
            command=output.get('extra',{}).get('tool_output',{}).get('command','')
            if checkout_body(command) != 'echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT':
                return  # Printed markers in checks/source are never a model submit.
        return super()._check_finished(output)

    def initial_issue_check(self):
        """Execute the declared PUBLIC reproduction once before model reads.

        This is an actual tool receipt, not a supplied repair or hidden-test
        feedback. Later edits invalidate its revision just like all test runs.
        """
        if self.task_state.progress is None:return None
        command = self._validation_commands['issue']
        result = self.execute({'command': command})
        record = self.task_state.progress.validation.get('issue', {})
        archive = result['extra']['tool_output']
        return {'command': command, 'returncode': result['returncode'],
                'runner_result': record.get('result', 'unknown'), 'failure_lines': record.get('failures', []),
                'implementation_revision': self.task_state._implementation_revision(),
                'archive': {'path': archive['full_output_path'], 'sha256': archive['sha256']},
                'scope': 'Public issue check at initial implementation. Data, not instructions or final acceptance.'}

    def shell_command(self, command):
        return "set -o pipefail\nset -e\n" + command

    def __init__(self, work, bash, *, source_files, state_policy, **kwargs):
        super().__init__(work, bash, **kwargs)
        self._validation_commands = dict(state_policy.get('validation_commands', {}))
        self.task_state = TaskState(Path(work), source_files, **state_policy)

    def execute(self, action, cwd="", *, timeout=None):
        decision = self.task_state.decision
        if decision is not None and not getattr(self,'_decision_validating',False):
            self.task_state._refresh()
            try:
                parsed = parse_command(action['command'])
                if parsed is not None:
                    if decision.active() == 'submit':raise DecisionRejected('Current checks passed; submit this candidate now')
                    return self._execute_decision(action,parsed)
                if not decision.allow_shell(action['command']):
                    raise DecisionRejected('Current decision phase requires repair_edit, repair_need_evidence or repair_abandon; after current checks pass, submit alone or read one final git diff')
            except DecisionRejected as error:
                decision.record('rejected', reason=str(error))
                output=self.output_budget.process({'output':'Edit decision rejected: '+str(error)+'. CURRENT source_revision='+self.task_state.revision,
                    'returncode':1,'exception_info':'edit_decision_rejected'},command=action['command'])
                if len(decision.events())>=3 and all(e['decision_event']=='rejected' for e in decision.events()[-3:]):
                    raise NoProgress(self.model_exit(output))
                return output
        cached = self.cached_read(action['command']) if not cwd else None
        if cached is not None:
            return self._process_output(cached, command=action['command'])
        decision = self.task_state.before_action(action["command"])
        if decision is not None:
            output = self.output_budget.process(decision["output"], command=action["command"])
            if decision["stop"]:
                raise NoProgress(self.model_exit(output))
            return output
        before=self.task_state._implementation_revision()
        result=super().execute(action, cwd, timeout=timeout)
        controller=self.task_state.decision
        if (controller is not None and not getattr(self,'_decision_validating',False)
                and self.task_state._implementation_revision()!=before):
            changed=[n for n in self.task_state.events[-1].get('changed_files_this_action',[])
                     if n.startswith(self.task_state.implementation_prefix)]
            controller.record('manual_edit_observed',changed_implementation_files=changed,
                note='Actual shell-authored source change; no hypothesis was inferred by the program.')
            return self._validate_decision_edit(action,result,', '.join(changed[:4]))
        return result

    def _execute_decision(self, action, parsed):
        name,value=parsed;decision=self.task_state.decision
        if name == 'repair_abandon':
            reason=value['reason']
            if not isinstance(reason,str) or not reason.strip() or len(reason.encode())>768:
                raise DecisionRejected('Supply a brief nonempty reason')
            decision.record('abandoned',reason=reason)
            raise NoProgress({'role':'exit','content':'RepairAbandoned','extra':{
                'exit_status':'NoProgress','submission':'','reason':'Model explicitly abandoned: '+reason}})
        if name in {'repair_need_evidence','repair_locate'}:
            result=decision.locate(value) if name=='repair_locate' else decision.evidence(value)
            return self._process_output({'output':json.dumps(result,ensure_ascii=False,separators=(',',':')),
                'returncode':0,'exception_info':'','extra':{'execution_kind':'decision_evidence'}},command=action['command'])
        result=decision.edit(value)
        edited=self._process_output({'output':json.dumps(result,ensure_ascii=False,separators=(',',':')),
            'returncode':0,'exception_info':'','extra':{'execution_kind':'structured_edit'}},command=action['command'])
        return self._validate_decision_edit(action,edited,result['path'])

    def _validate_decision_edit(self,action,edited,path):
        decision=self.task_state.decision
        receipts=[]
        self._decision_validating=True
        try:
            for scope,command in self._validation_commands.items():
                check=self.execute({'command':command},timeout=self.config.timeout)
                record=self.task_state.progress.validation.get(scope,{})
                receipts.append({'scope':scope,'returncode':check['returncode'],'result':record.get('result','unknown'),
                    'output':check['extra']['tool_output']['full_output_path'],'failures':record.get('failures',[])[:2]})
        finally:self._decision_validating=False
        body={'edit':{'status':'changed','path':path,'output':edited['extra']['tool_output']['full_output_path']},
            'source_revision':self.task_state.revision,'checks':receipts,'next_phase':decision.active(),
            'note':'Actual checks; independent acceptance and standalone model submission still required.'}
        while len(json.dumps(body,ensure_ascii=False,separators=(',',':')).encode())>self.output_budget.max_output_bytes:
            longest=max(receipts,key=lambda r:sum(len(t) for t in r['failures']))
            if not longest['failures']:break
            longest['failures'].pop();longest['details_omitted_see_output']=True
        return self.output_budget.process({'output':json.dumps(body,ensure_ascii=False,separators=(',',':')),
            'returncode':next((r['returncode'] for r in receipts if r['returncode']!=0),0),'exception_info':'',
            'extra':{'execution_kind':'edit_validation_receipt'}},command=action['command'])

    def cached_read(self, command):
        """Advance an immutable read, or recover one archived observation.

        Only a proved read-only plan is eligible. Never execute a shell action
        here, never count this as new source/validation progress. Records live
        in ordinary tool-output events, so checkpoint import needs no new state.
        """
        state = self.task_state
        state._refresh()
        plan = state.read_plan(command)
        if not plan:return None
        revision = state._implementation_revision()
        events = self.output_budget.events
        previous = next((e for e in reversed(events) if e.get('read_key') == plan['key']
                         and e.get('read_revision') == revision), None)
        recovery = False
        if previous and previous.get('page_next_line'):
            start = previous['page_next_line']
        else:
            # Already delivered observations may have left the API view. Offer
            # one bounded recovery, not unlimited rereads after every compaction.
            if state.visible_outputs is None:return None
            if previous is None and plan['single_span']:
                name, first, last = plan['spans'][0]
                previous = next((e for e in reversed(events) if e.get('read_revision') == revision
                    and e.get('source_base') and e['source_base'][0] == name
                    and e['source_base'][1] <= first and last <= e['source_base'][2]
                    and e['full_output_path'] not in state.visible_outputs), None)
            if previous is None or previous['full_output_path'] in state.visible_outputs:return None
            origin = previous.get('page_origin', previous['full_output_path'])
            if any(e.get('execution_kind') == 'archived_recovery' and e.get('page_origin') == origin
                   and e.get('read_revision') == revision for e in events):return None
            start = 1;recovery = True
            if plan['single_span'] and previous.get('source_base'):
                start = plan['spans'][0][1] - previous['source_base'][1] + 1
        origin = previous['page_origin']
        path = self.output_budget.work / origin
        if not path.is_file() or not path.resolve().is_relative_to(self.output_budget.work):
            # Checkpoint history is not a capability to read an old checkout.
            # Let the normal guarded source action acquire current evidence.
            return None
        raw = path.read_bytes()
        import hashlib
        if hashlib.sha256(raw).hexdigest() != previous['page_origin_sha256']:
            raise RuntimeError('Read archive fingerprint changed')
        lines = raw.decode('utf-8').splitlines(keepends=True)
        stop = min(previous.get('page_stop_line', len(lines)), len(lines))
        base = previous.get('source_base')
        if recovery and plan['single_span'] and base:
            stop = min(stop, plan['spans'][0][2] - base[1] + 1)
        if not 1 <= start <= stop:return None
        metadata = {'read_key': plan['key'], 'read_revision': revision, 'page_origin': origin,
            'page_origin_sha256': previous['page_origin_sha256'], 'page_start_line': start, 'page_stop_line': stop,
            'execution_kind': 'archived_recovery' if recovery else 'archive_continuation'}
        if base:metadata['source_base'] = base
        return {'output': ''.join(lines[start-1:stop]), 'returncode': 0, 'exception_info': '',
                'extra': {'read_page': metadata}}

    @staticmethod
    def model_exit(output):
        return {"role": "exit", "content": "NoProgress", "extra": {
            "exit_status": "NoProgress", "submission": "", "reason": output["output"]}}

    def _process_output(self, output, *, command):
        plan = self.task_state.read_plan(command)
        span = plan['spans'][0] if plan and plan['single_span'] and output['returncode'] == 0 else None
        metadata = output.get('extra', {}).get('read_page')
        if metadata and metadata.get('source_base'):
            name, first, last = metadata['source_base']
            span = (name, first + metadata['page_start_line']-1, min(last, first+metadata['page_stop_line']-1))
        elif metadata:span = None
        if metadata is None and plan and output['returncode'] == 0:
            metadata = {'read_key': plan['key'], 'read_revision': self.task_state._implementation_revision(),
                        'execution_kind': 'shell'}
        result = self.output_budget.process(output, command=command, read_span=span, read_metadata=metadata)
        self.task_state.observe(command, output, result["extra"]["tool_output"])
        return result


class StatefulModel(ContextManagedModel):
    def __init__(self, *args, task_state, **kwargs):
        super().__init__(*args, **kwargs)
        self.task_state = task_state

    def _perform_completion(self,messages,provider):
        decision=getattr(self.task_state,'decision',None)
        if decision is None or decision.active()!='decide':
            return super()._perform_completion(messages,provider)
        # All shared provider/accounting/deadline guards have already executed.
        # Narrow the actual API tool set, rather than asking bash to print a plan.
        import litellm
        from edit_decision import api_tools
        self.request_check()
        remaining=self.request_deadline()-time.monotonic() if self.request_deadline else 45
        if remaining<=0:raise TimeoutError('No edit-decision request time remaining')
        request=self.protocol_adapter.prepare(messages,api_tools(),model=self.config.model_name,
            api_key=self._api_key,**dict(self.config.model_kwargs,timeout=min(45,remaining)))
        return litellm.completion(**request)

    def _parse_actions(self,response):
        calls=response.choices[0].message.tool_calls or []
        if any(c.function.name in PREFIXES for c in calls):
            from minisweagent.exceptions import FormatError
            try:
                if self.task_state.decision is None or len(calls)!=1 or not calls[0].id:
                    raise DecisionRejected('Use one registered decision tool in the initial repair only')
                call=calls[0]
                if call.function.name not in PREFIXES:raise DecisionRejected('Unregistered decision tool')
                command=call.function.name+' '+call.function.arguments
                parse_command(command)  # Same strict JSON and size contract; no argument repair.
            except (ValueError,TypeError) as error:
                raise FormatError({'role':'user','content':'Invalid edit tool: '+str(error),
                    'extra':{'interrupt_type':'FormatError'}}) from None
            return [{'command':command,'tool':call.function.name,'tool_call_id':call.id}]
        return super()._parse_actions(response)

    def project_context(self, messages, prepared, pinned=None):
        pinned=list(pinned or [])
        decision=getattr(self.task_state,'decision',None)
        message=decision.message() if decision is not None else None
        if message:pinned.append(message)
        return super().project_context(messages,prepared,pinned)

    def finalize_decision_view(self, view):
        decision=getattr(self.task_state,'decision',None)
        message=decision.message() if decision is not None else None
        if message:return [m for m in view if m != message]+[message]
        return view

    def _prepare_messages_for_api(self, messages):
        prepared = RecordedModel._prepare_messages_for_api(self, messages)
        state = self.task_state.pinned_message()
        # Add only to the API view, after the original system/task messages.
        projected = self.project_context(messages, prepared, [state])
        prepared_tools = {m['tool_call_id']: m.get('content') for m in prepared if m.get('role') == 'tool'}
        visible_ids = {m['tool_call_id'] for m in projected if m.get('role') == 'tool'
                       and m.get('content') == prepared_tools.get(m['tool_call_id'])}
        self.task_state.visible_outputs = {m['extra']['tool_output']['full_output_path'] for m in messages
            if m.get('role') == 'tool' and m.get('tool_call_id') in visible_ids
            and m.get('extra', {}).get('tool_output')}
        return self.finalize_decision_view(projected)


def evaluate(name: str, work: Path, output: Path) -> dict:
    result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/check_task.py"), "--task", name,
                            "--repo", str(work), "--output", str(output), "--full-suite"],
                            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=240)
    if not output.is_file():
        raise RuntimeError("Independent evaluation did not produce a report: exit " + str(result.returncode))
    report = json.loads(output.read_text(encoding="utf-8"))
    expected = 0 if report["task_tests_passed"] and report["full_upstream_suite"]["successful"] else 1
    if result.returncode != expected:
        raise RuntimeError("Evaluator exit/report mismatch.")
    return report


def write_repair_patch(path: Path, lines) -> None:
    # Git patch format needs LF even when this exporter runs on Windows.
    path.write_bytes("".join(lines).encode("utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--config", default="local_baseline_20261007.json",
                        choices=("local_baseline_20261007.json", "output_budget_trial_20261007.json", "task_state_trial_20261008.json", "history_retention_trial_20261008.json", "new_task_evaluation_20261010.json", "execution_progress_trial_20261010.json", "repository_retrieval_dev_20261014.json", "repository_retrieval_validation_20261014.json", "retrieval_delivery_trial_20261015.json"))
    parser.add_argument("--variant", choices=("raw", "bounded", "context_managed", "context_compacted", "context_complete", "state_only", "state_guard", "state_guard_v2", "state_guard_v3", "adaptive_history", "execution_progress", "repository_retrieval", "retrieval_on_change"), default="raw")
    args = parser.parse_args()
    config = json.loads((ROOT / "configs" / args.config).read_text(encoding="utf-8"))
    freeze = verify_freeze(ROOT, config["freeze_manifest"]) if config.get("freeze_manifest") else None
    if args.config in {"new_task_evaluation_20261010.json", "execution_progress_trial_20261010.json", "repository_retrieval_dev_20261014.json", "repository_retrieval_validation_20261014.json", "retrieval_delivery_trial_20261015.json"} and freeze is None:
        raise RuntimeError("This evaluation requires predeclared frozen inputs.")
    variants = config.get("variants", {"raw": {"tool_output_budget": None}})
    if args.variant not in variants:
        raise SystemExit("Variant is not configured for this batch.")
    output_policy = variants[args.variant]["tool_output_budget"]
    context_policy = variants[args.variant].get("context_budget")
    archive_assistant = variants[args.variant].get("archive_assistant_content", False)
    state_policy = variants[args.variant].get("task_state")
    retrieval_policy = variants[args.variant].get("repository_retrieval")
    if retrieval_policy is not None and context_policy is None:
        raise ValueError("Retrieval requires context reservation.")
    if args.task not in config["tasks"]:
        raise SystemExit("Task is not in this fixed batch.")
    identity = config["batch_id"] + "_" + args.task
    if "variants" in config:
        identity += "_" + args.variant
    claim = ROOT / "temp" / (identity + "_claim.json")
    if args.run and claim.exists():
        raise SystemExit("This paid task run already started; preserve its evidence.")
    work, manifest = prepare_task(args.task)
    import_directory = package_root(work, manifest) if manifest.get("import_root") else None
    issue_command = manifest.get("agent_issue_command", "python -B -m unittest discover -s task_checks -v")
    if state_policy is not None and state_policy.get("progress_policy") is not None:
        state_policy = state_policy | {"implementation_prefix": manifest["source_prefix"],
            "validation_commands": {"targeted": manifest["agent_test_command"],
                                    "issue": issue_command}}
    results = ROOT / "temp" / (identity + "_" + datetime.now().strftime("%H%M%S_%f"))
    results.mkdir()
    baseline = evaluate(args.task, work, results / "baseline.json")
    if baseline["issue_checks"]["successful"] or not baseline["upstream_targeted"]["successful"]:
        raise RuntimeError("Expected reproduced issue with passing upstream targeted tests.")
    settings = dotenv_values(ROOT / ".env", interpolate=False)
    model_options = {"ledger_path": results / "ledger.json", "error_path": results / "request_error.json"}
    retrieval = (RepositoryRetrieval(work, manifest["source_files_sha256"], source_prefix=manifest["source_prefix"],
                    query=(ROOT / "tasks" / args.task / "issue.md").read_text(encoding="utf-8"), **retrieval_policy)
                 if retrieval_policy is not None else None)
    model_options["repository_retrieval"] = retrieval
    state_environment = (StatefulTaskEnvironment(work, bash_executable(),
                         source_files=list(manifest["source_files_sha256"]), state_policy=state_policy,
                         python_import_root=import_directory, **output_policy)
                         if state_policy is not None else None)
    model = (StatefulModel(settings, task_state=state_environment.task_state, context_policy=context_policy, **model_options) if state_environment is not None
             else ContextManagedModel(settings, context_policy=context_policy, **model_options) if context_policy is not None
             else RecordedModel(settings, **model_options))
    if archive_assistant:
        model.context_budget.enable_assistant_archives(work)
    environment = TaskEnvironment(work, bash_executable(), python_import_root=import_directory)
    probe = environment.execute({"command": "git rev-parse --show-toplevel; python -B -c 'import sys; print(sys.prefix)'"})
    if probe["returncode"] != 0 or str(ROOT / ".venv").lower() not in probe["output"].lower():
        raise RuntimeError("Git Bash / virtual environment preflight failed.")
    # This assertion uses the real shell and must fail the pipeline overall.
    pipeline = environment.execute({"command": "python -B -c 'raise SystemExit(7)' | tail -1"})
    if pipeline["returncode"] == 0:
        raise RuntimeError("Pipeline hid an earlier command failure.")
    package_probe = "import importlib; print(importlib.import_module(" + repr(manifest["package"]) + ").__file__)"
    imported = environment.execute({"command": "python -B -c " + shlex.quote(package_probe)})
    if imported["returncode"] != 0 or not Path(imported["output"].strip()).resolve().is_relative_to(work.resolve()):
        raise RuntimeError("Agent shell imported a package outside its task checkout.")
    if model.contains_credentials(json.dumps(model.serialize())):
        raise RuntimeError("Credentials appeared in serialized configuration.")
    preflight = {"task_id": manifest["task_id"], "work": str(work), "git_root_verified": True,
                 "pipefail_verified": True, "variant": args.variant, "tool_output_budget": output_policy,
                 "context_budget": context_policy,
                 "archive_assistant_content": archive_assistant,
                 "task_state": state_policy,
                 "external_model_requests": 0, "budget": model.budget.snapshot(), "evaluation_freeze": freeze}
    preflight["provider_routing"] = model.provider_statistics()
    preflight["task_package_import_verified"] = True
    preflight["python_import_root"] = str(import_directory) if import_directory else None
    preflight["repository_retrieval"] = retrieval_policy
    preflight["retrieval_preview"] = retrieval.pinned_message(preview=True) if retrieval is not None else None
    (results / "preflight.json").write_text(json.dumps(preflight, indent=2), encoding="utf-8")
    if not args.run:
        print(json.dumps(preflight, indent=2))
        print("Temporary results: " + str(results))
        return
    with claim.open("x", encoding="utf-8") as stream:
        json.dump({"results": str(results), "work": str(work), "task": args.task}, stream, indent=2)
    if output_policy is not None:
        environment = state_environment or BoundedTaskEnvironment(work, bash_executable(), python_import_root=import_directory, **output_policy)
    current_config = config | {"variant": args.variant, "tool_output_budget": output_policy, "context_budget": context_policy,
                               "archive_assistant_content": archive_assistant,
                               "task_state": state_policy,
                               "repository_retrieval": retrieval_policy,
                               "model_name": model.config.model_name, "provider_routing": model.provider_statistics(), "task_manifest": manifest,
                               "budget": model.budget_config, "step_limit": 0, "evaluation_freeze": freeze}
    (results / "run_config.json").write_text(json.dumps(current_config, indent=2), encoding="utf-8")
    for name in ("run_task_batch.py", "task_workspace.py", "check_task.py", "run_deepseek_smoke.py", "deepseek_budget.py", "tool_output_budget.py", "context_budget.py", "task_state.py", "state_agent.py", "evaluation_freeze.py", "execution_progress.py", "provider_priority.py", "repository_retrieval.py"):
        shutil.copy2(ROOT / "scripts" / name, results / ("snapshot_" + name))
    shutil.copy2(ROOT / "configs/provider_priority.json", results / "provider_priority.json")
    for name in ("issue.md", "checks/test_behavior.py", "manifest.json"):
        shutil.copy2(ROOT / "tasks" / args.task / name, results / Path(name).name)
    state_instruction = ("\nA bounded program-recorded task state is provided on every request. Use it instead of repeating old reads. "
        "When your diagnosis or plan changes, write a short JSON object with string fields hypothesis and next_action "
        "to temp/task-state/notes.json using an available shell/Python command. These are your unverified notes, "
        "not accepted test results. If a preview omits relevant code, read a narrower range or the archived full output. "
        "Proceed to a concrete reproduction or repair when enough evidence exists.\n" if state_policy is not None else "")
    agent_class = StatefulAgent if state_policy is not None else DefaultAgent
    agent = agent_class(model, environment,
        system_template="You are a helpful software engineering assistant. Use exactly one bash tool call per response.",
        instance_template=("Fix this issue: {{task}}\n"
            "Work only in this checkout. It has an independent Git baseline commit. Do not access parent directories, "
            "unrelated projects, the web, or install dependencies. Do not delete files. Put temporary helpers in temp/. "
            "Do not edit tests/, task_checks/, TASK.md, or project metadata. Change only implementation files. "
            "Search for relevant symbols and read short line ranges; avoid whole large files or long test output. "
            "Inspect, reproduce, repair, then run both:\n" + manifest["agent_test_command"] + "\n"
            + issue_command + "\n"
            "Commands run in fresh Git Bash shells on Windows, using the available virtualenv Python. "
            "Shell pipelines propagate failures. When finished, run echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT by itself." + state_instruction),
        step_limit=0, cost_limit=model.budget.agent_cost_limit, wall_time_limit_seconds=config["wall_time_limit_seconds"],
        output_path=results / "trajectory.json")
    start = time.monotonic()
    try:
        status = agent.run((ROOT / "tasks" / args.task / "issue.md").read_text(encoding="utf-8"))
    except Exception as error:
        status = {"exit_status": type(error).__name__}
    finally:
        model.ledger.update(completed=True, note="This run ended. Shared total budget remains cumulative.")
        (results / "ledger.json").write_text(json.dumps(model.ledger, indent=2), encoding="utf-8")
    elapsed = time.monotonic() - start
    if freeze is not None:
        verify_freeze(ROOT, config["freeze_manifest"], expected_digest=freeze["sha256"])
    load_task(args.task)  # Frozen external inputs must still be intact.
    verification = evaluate(args.task, work, results / "verification.json")
    changed = changed_files(work, manifest)
    task, _ = load_task(args.task)
    protected = all((work / ("TASK.md" if relative == "issue.md" else "task_checks/test_behavior.py")).is_file()
                    and digest(work / ("TASK.md" if relative == "issue.md" else "task_checks/test_behavior.py")) == expected
                    for relative, expected in manifest["task_files_sha256"].items())
    unexpected = [relative for relative in changed if not relative.startswith(manifest["source_prefix"])]
    patch = []
    for relative in changed:
        if relative not in manifest["source_files_sha256"]:
            continue
        before = (task / "repo" / relative).read_text(encoding="utf-8").splitlines(keepends=True)
        after = (work / relative).read_text(encoding="utf-8").splitlines(keepends=True) if (work / relative).is_file() else []
        patch.extend(difflib.unified_diff(before, after, fromfile="a/" + relative, tofile="b/" + relative))
    patch_path = results / "repair.diff"
    write_repair_patch(patch_path, patch)
    before_full, after_full = baseline["full_upstream_suite"], verification["full_upstream_suite"]
    new_failures = sorted(set(after_full["failures"] + after_full["errors"]) - set(before_full["failures"] + before_full["errors"]))
    replay_passed = False
    if patch and not unexpected and protected and verification["task_tests_passed"] and not new_failures:
        replay, _ = prepare_task(args.task)
        subprocess.run(["git", "-C", str(replay), "apply", "--", str(patch_path)], check=True, capture_output=True)
        replay_result = evaluate(args.task, replay, results / "patch_verification.json")
        replay_full = replay_result["full_upstream_suite"]
        replay_new = set(replay_full["failures"] + replay_full["errors"]) - set(before_full["failures"] + before_full["errors"])
        replay_passed = replay_result["task_tests_passed"] and not replay_new
    summary = {"task_id": manifest["task_id"], "variant": args.variant, "exit_status": status.get("exit_status"),
               "provider_failure": model.ledger.get("provider_failure"),
               "provider_routing": model.provider_statistics(),
               "limit_kind": status.get("limit_kind"), "work": str(work),
               "requests": model.requests, "estimated_cost_cny": model.complete_cost_estimate(),
               "known_price_estimate_cny": model.actual_cost_cny, "usage": model.usage,
               "request_measurements": model.request_measurements,
               "tool_output_statistics": environment.output_budget.statistics() if output_policy is not None else None,
               "context_projections": model.context_budget.events if context_policy is not None else None,
               "task_state": environment.task_state.facts() if state_policy is not None else None,
               "pinned_state_requests": environment.task_state.pinned_requests if state_policy is not None else None,
               "repository_retrieval": {"policy": retrieval_policy, "projections": retrieval.events} if retrieval is not None else None,
               "elapsed_seconds": round(elapsed, 3), "changed_files": changed, "unexpected_changes": unexpected,
               "checks_and_description_unchanged": protected, "task_tests_passed": verification["task_tests_passed"],
               "new_full_suite_failures": new_failures, "patch_replay_passed": replay_passed,
               "repair_verified": verification["task_tests_passed"] and protected and not unexpected and not new_failures and replay_passed,
               "shared_budget": model.budget.snapshot(), "scope": manifest["scope"]}
    (results / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if output_policy is not None:
        shutil.copytree(environment.output_budget.directory, results / "tool-output")
    if archive_assistant:
        shutil.copytree(model.context_budget.history_directory, results / "context-history")
    if state_policy is not None:
        shutil.copytree(environment.task_state.directory, results / "task-state")
    if retrieval is not None:
        shutil.copytree(retrieval.directory, results / "repository-retrieval")
    if any(model.contains_credentials(p.read_text(encoding="utf-8")) for p in results.rglob("*") if p.is_file()):
        raise RuntimeError("Credentials found in results; do not publish.")
    print(json.dumps({k: summary[k] for k in ("task_id", "variant", "exit_status", "provider_failure", "provider_routing", "requests", "estimated_cost_cny", "repair_verified", "shared_budget")}, ensure_ascii=False, indent=2))
    print("Temporary results: " + str(results))


if __name__ == "__main__":
    main()
