"""C integration: protected tool wire, receipts, continuation and deadlines."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
import uuid

ROOT = Path(__file__).resolve().parent.parent
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["MSWEA_GLOBAL_CONFIG_DIR"] = str(ROOT / "temp/mini-global")
os.environ["MSWEA_SILENT_STARTUP"] = "1"
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

from litellm import ModelResponse
from collaboration import CollaborationCoordinator, RoleContext, attributed_usage, role_identity
from collaboration_live import LiveRunner, verification_summary
from collaboration_protocol import ProtocolError, ReviewReport, canonical_bytes, sha256
from collaboration_runtime import (LiveRoleBudget, ProtectedReviewModel, RepairModel, RepairSession, RepairEnvironment, run_review)
from collaboration_store import CollaborationStopped, CollaborationStore
from deepseek_budget import SharedBudget, load_budget_config
from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import FormatError, LimitsExceeded, Submitted
from minisweagent.models.test_models import DeterministicToolcallModel, make_toolcall_output
from review_model import REVIEW_TOOLS
from run_deepseek_smoke import BudgetedDeepSeekModel, GitBashEnvironment
from task_service_store import policy
from state_agent import NoProgress
from test_collaboration_protocol import StageAFixture
from test_collaboration_store import make_store, claim_first
import test_provider_priority as priority_tests


def response(tool="read_diff", arguments=None, content=None):
    return ModelResponse(id="offline-response", model="offline-model", choices=[{"index": 0,
        "message": {"role": "assistant", "content": content, "tool_calls": [{"id": "call-" + uuid.uuid4().hex,
            "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments or {})}}]}, "finish_reason": "tool_calls"}],
        usage={"prompt_tokens": 100, "completion_tokens": 20})


class RuntimeTests(unittest.TestCase):
    def test_review_range_and_field_errors_give_exact_nonexecuting_corrections(self):
        f=StageAFixture();c=f.snapshot.candidate;doc=f.doc()
        model=self.model()
        common={'snapshot_id':c.snapshot_id,'document_id':doc.document_id}
        report=ReviewReport(review_id='corrected-review',run_id=c.run_id,reviewer_role_run_id=f.reviewer.role_run_id,
            candidate_id=c.candidate_id,source_tree_digest=c.source_tree_digest,verdict='approve',coverage=('Offline protocol fixture',))
        calls=[response('read_lines',common|{'start':1,'end':81}),
            response('read_lines',common|{'start_line':1,'end_line':2}),
            response('read_lines',common|{'start':1,'end':2}),
            response('submit_review',{'report':report.model_dump(mode='json')})]
        output=self.store.home/'review-parameter-correction.json'
        with patch('review_model.litellm.completion',side_effect=calls),patch.object(f.broker,'dispatch',wraps=f.broker.dispatch) as dispatch:
            run_review(model,f.broker,SimpleNamespace(check=lambda:None,identity=f.reviewer),output,'offline')
        messages=json.loads(output.read_bytes())['messages']
        rejection=next(json.loads(m['content']) for m in messages if m['role']=='tool' and 'max_lines_per_read' in m['content'])
        self.assertEqual(rejection['requested_line_count'],81)
        self.assertEqual(rejection['max_lines_per_read'],80)
        self.assertEqual(rejection['selected_document']['line_count'],2)
        self.assertEqual(rejection['example_only_not_executed']['end'],2)
        self.assertLessEqual(len(canonical_bytes(rejection)),4096)
        self.assertEqual(len(f.broker.evidence),1) # Only the corrected actual read minted evidence.
        self.assertEqual(dispatch.call_count,3) # Bad field names never dispatched.
        correction=next(m['content'] for m in messages if m['role']=='user' and 'required_input_fields' in m['content'])
        self.assertIn('start_line',correction);self.assertIn('"start"',correction)
        self.assertNotIn('"input":',correction)
        # Permissions cannot be turned into range metadata for another document.
        from review_tools import review_rejection
        self.assertNotIn('selected_document',review_rejection(ProtocolError('document_outside_handoff_scope')))

    def controlled_repair_environment(self):
        work=ROOT/'temp/repair_contract_tests'/uuid.uuid4().hex
        files={'pkg/__init__.py':'','pkg/code.py':'def value(): return 0\n',
            'tests/test_code.py':'from pkg.code import value\ndef test_value(): assert value()==1\n',
            'task_checks/test_behavior.py':'import unittest\nfrom pkg.code import value\nclass Cases(unittest.TestCase):\n def test_value(self): self.assertEqual(value(),1)\n'}
        for name,text in files.items():
            p=work/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text,encoding='utf-8')
        context=SimpleNamespace(check=lambda:None,deadline=time.monotonic()+60,store=SimpleNamespace(event=Mock()),
            identity=SimpleNamespace(run_id='contract-run',role_run_id='repair-1'))
        from execution_runtime import bash_executable
        env=RepairEnvironment(work,bash_executable(),context=context,source_files=list(files),max_output_bytes=4096,
            python_import_root=work,state_policy={'guard_enabled':True,'implementation_prefix':'pkg/',
                'progress_policy':{'stagnation_actions':8},'validation_commands':{
                    'targeted':'python -B -m pytest tests/test_code.py -q -p no:cacheprovider',
                    'issue':'python -B -m unittest discover -s task_checks -v'}})
        env.model=SimpleNamespace(contains_credentials=lambda _:False)
        return work,env,context

    def test_repair_compound_checks_have_actual_exits_and_require_real_submission(self):
        work,env,context=self.controlled_repair_environment()
        chain='cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"; python -B -m pytest tests/test_code.py -q -p no:cacheprovider; echo "PYTEST_RC=$?"; python -B -m unittest discover -s task_checks -v; echo "UNITTEST_RC=$?"'
        result=env.execute({'command':chain})
        self.assertNotEqual(result['returncode'],0)
        self.assertEqual(env.task_state.progress.facts()['current_validation'],{'targeted':'failed','issue':'failed'})
        fix="from pathlib import Path; Path('pkg/code.py').write_text('def value(): return 1\\n')"
        env.execute({'command':'python -B -c '+shlex.quote(fix)})
        result=env.execute({'command':chain})
        self.assertEqual(result['returncode'],0)
        self.assertEqual(env.task_state.progress.facts()['phase'],'review_and_submit')
        actions=env.task_state.progress.actions
        blocked=env.execute({'command':chain})
        self.assertIn('Submit this candidate now',blocked['output'])
        self.assertEqual(env.task_state.progress.actions,actions)
        self.assertIsNone(env.completed_submission)
        with self.assertRaises(Submitted):env.execute({'command':'echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT'})
        self.assertIsNotNone(env.completed_submission)
        env.reset_repair_control()
        self.assertEqual(env.execute({'command':'cat pkg/code.py'})['returncode'],0)
        # A new revision phase must not inherit the old submit-only barrier.
        context.identity.role_run_id='repair-2'
        self.assertEqual(env.execute({'command':'cat pkg/code.py'})['returncode'],0)

    def test_repair_overlapping_reads_are_bounded_but_new_lines_and_edits_remain_possible(self):
        work,env,context=self.controlled_repair_environment()
        (work/'pkg/code.py').write_text('\n'.join('# line '+str(i) for i in range(50)),encoding='utf-8')
        env.execute({'command':"sed -n '1,30p' pkg/code.py"})
        env.execute({'command':"sed -n '2,20p' pkg/code.py"})
        for i in range(2):
            result=env.execute({'command':"sed -n '3,15p' pkg/code.py"})
            self.assertIn('already available',result['output'])
        # A genuinely unread range is allowed and resets consecutive rejection.
        self.assertEqual(env.execute({'command':"sed -n '31,40p' pkg/code.py"})['returncode'],0)
        for i in range(2):env.execute({'command':"sed -n '3,15p' pkg/code.py"})
        with self.assertRaises(NoProgress):env.execute({'command':"sed -n '3,15p' pkg/code.py"})
        (work/'pkg/code.py').write_text('new implementation\n',encoding='utf-8')
        self.assertEqual(env.execute({'command':"sed -n '1,30p' pkg/code.py"})['returncode'],0)

    def test_truncated_read_does_not_cover_missing_source_and_middle_remains_readable(self):
        work,env,context=self.controlled_repair_environment()
        text='\n'.join('# '+str(i)+' '+('x'*150) for i in range(100))+'\n'
        (work/'pkg/code.py').write_text(text,encoding='utf-8')
        wide="sed -n '1,90p' pkg/code.py"
        output=env.execute({'command':wide})
        self.assertTrue(output['extra']['tool_output']['truncated'])
        # Only the complete contiguous PAGE is delivered; the rest is missing.
        delivered=output['extra']['tool_output']['delivered_source_span']
        self.assertEqual(delivered[:2],['pkg/code.py',1])
        self.assertLess(delivered[2],40)
        self.assertIsNone(env._covered_read(('pkg/code.py',40,44)))
        # These middle lines were absent from HEAD/TAIL; they must not be blocked.
        narrow=env.execute({'command':"sed -n '40,44p' pkg/code.py"})
        self.assertEqual(narrow['returncode'],0)
        self.assertFalse(narrow['extra']['tool_output']['truncated'])
        self.assertIn('# 41 ',narrow['output'])
        self.assertEqual(env._read_spans['pkg/code.py'][-1][:2],(40,44))
        env.execute({'command':wide})
        rejected=env.execute({'command':wide})
        self.assertIn('truncated output does NOT mean',rejected['output'])
        self.assertIsNone(env.completed_submission)

    def test_compound_search_and_archive_loops_close_but_real_progress_remains_open(self):
        work,env,context=self.controlled_repair_environment()
        (work/'pkg/code.py').write_text('def value(): return 0\n'+'# unused\n'*50,encoding='utf-8')
        compound='cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" && sed -n 1,4p pkg/code.py && echo ---- && sed -n 30,34p pkg/code.py'
        env.execute({'command':compound});env.execute({'command':compound})
        rejected=env.execute({'command':compound.replace('----','different separator')})
        self.assertIn('Repeated read-only request',rejected['output'])
        search='grep -n "value\\|unused" pkg/code.py | head -60'
        env.execute({'command':search});env.execute({'command':search})
        env.execute({'command':search})
        archive='temp/tool-output/0001.txt'
        # Allowed immutable archive read can supply real, previously missing evidence.
        self.assertEqual(env.execute({'command':f"sed -n '1,2p' {archive}"})['returncode'],0)
        # Varying broad reads after actual stagnation cannot reopen exploration.
        while not env.task_state.progress.facts()['stagnation_feedback']:
            env.execute({'command':'python -B -c "print(0)"'})
        blocked=env.execute({'command':'grep -n "value" pkg/code.py | head -10'})
        self.assertIn('Read-only exploration has stalled',blocked['output'])
        missing=env.execute({'command':"sed -n '15,16p' pkg/code.py"})
        self.assertEqual(missing['returncode'],0)
        recovery=env.execute({'command':'cat '+missing['extra']['tool_output']['full_output_path']})
        self.assertEqual(recovery['returncode'],0)
        recursive=env.execute({'command':'cat '+recovery['extra']['tool_output']['full_output_path']})
        self.assertIn('Read-only exploration has stalled',recursive['output'])
        # The failure is obtained from the actual registered runner, not a claim.
        issue=env.execute({'command':'python -B -m unittest discover -s task_checks -v'})
        self.assertNotEqual(issue['returncode'],0)
        self.assertEqual(env.task_state.progress.facts()['current_validation']['issue'],'failed')
        fix="from pathlib import Path; Path('pkg/code.py').write_text('def value(): return 1\\n')"
        env.execute({'command':'python -B -c '+shlex.quote(fix)})
        self.assertEqual(env.execute({'command':'cat pkg/code.py'})['returncode'],0)
        self.assertIsNone(env.completed_submission)

    def test_read_classifier_declines_writes_and_never_certifies_filtered_source_coverage(self):
        work,env,context=self.controlled_repair_environment();env._sync_control()
        for command in ["sed -i 's/0/1/' pkg/code.py",'cat pkg/code.py > pkg/new.py',
                'sed -n 1,3p pkg/code.py && python modify.py','grep --pre=touch value pkg/code.py',
                'cat ../outside.py','cat "$(pwd)/pkg/code.py"','cd /outside && cat pkg/code.py',
                "sed -n '1,3p' pkg/code.py || true",'cat pkg/code.py &']:
            with self.subTest(command=command):self.assertIsNone(env._read_plan(command))
        self.assertEqual(env._read_plan('cat pkg/code.py | head -1')['spans'],[])
        self.assertFalse(env._small_missing_read(env._read_plan("sed -n '1000,1002p' pkg/code.py")))
        # A quote containing a semicolon is data, not a second shell command.
        self.assertIsNotNone(env._read_plan('grep -n "x;y" pkg/code.py | head -3'))
        (work/'temp/tool-output/unregistered.txt').write_text('not issued',encoding='utf-8')
        self.assertIsNone(env._read_plan('cat temp/tool-output/unregistered.txt'))

    def test_repair_stop_reason_survives_empty_candidate_and_retains_partial_snapshot(self):
        # Reuse the actual runner ordering, with no model or subprocess. A valid
        # partial candidate is still sealed, but a stopped repair never advances.
        runner = LiveRunner.__new__(LiveRunner)
        runner.run_id = 'synthetic-stop'
        runner.issue = b'synthetic issue'
        runner.store = SimpleNamespace(get=lambda _: {}, event=Mock())
        runner.store.state = lambda _: {'collaboration_state': {'review_id': 'review'}}
        runner.store.records = lambda _, kind: {'review': object()} if kind == 'review' else {}
        runner.diagnostic = False
        runner.self_review = False
        runner.work, runner.manifest = ROOT / 'temp/read_guard_20261027', {}
        runner.make_repair_session = Mock()
        context = SimpleNamespace(role={'phase': 'repairing'}, check=lambda: None,
                                  identity=SimpleNamespace(role_run_id='repair-role'))
        with patch('collaboration_live.repair_feedback', return_value='feedback'):
            for phase in ['repairing', 'revising']:
                context.role['phase'] = phase
                for status, name in [('NoProgress', 'RepairNoProgress'), ('ProviderUnavailable', 'RepairProviderUnavailable')]:
                    result = {'exit_status': status}
                    runner.session = SimpleNamespace(start=lambda _: result, continue_with_feedback=lambda *_: result)
                    for error in [ProtocolError('no_nonempty_candidate'), None]:
                        runner.seal = Mock(side_effect=error) if error else Mock(return_value=object())
                        with self.assertRaises(RuntimeError) as caught:
                            runner.repair(context)
                        self.assertEqual(type(caught.exception).__name__, name)
                        runner.seal.assert_called_once_with(context)
                    # Integrity/provenance failures must remain visible as such.
                    runner.seal = Mock(side_effect=ProtocolError('protected_file_changed'))
                    with self.assertRaisesRegex(ProtocolError, 'protected_file_changed'):
                        runner.repair(context)

    def setUp(self):
        self.store, self.parent = make_store()
        self.role = claim_first(self.store, self.parent)
        self.context = RoleContext(self.store, self.role, time.monotonic() + 30)
        self.path = self.store.home / "synthetic-accounting.json"
        self.path.write_bytes(canonical_bytes({"historical_estimated_cost_cny": "7.25", "historical_unknown_charge_reservation_cny": "0.20",
            "blocked": False, "requests": {"old": {"status": "reserved", "amount_cny": "0.10"}}}))
        self.accounting = SharedBudget(load_budget_config(), ledger_path=self.path, temp_dir=self.store.home / "locks")
        self.settings = {
            "BUPT_API_KEY": "primary-test-secret", "BUPT_API_BASE_URL": "https://myai.bupt.edu.cn/llm-gw/v1", "BUPT_CHAT_MODEL": "deepseek-v4-flash",
            "DEEPSEEK_API_KEY": "backup-test-secret", "DEEPSEEK_API_BASE_URL": "https://api.deepseek.com", "DEEPSEEK_CHAT_MODEL": "deepseek-flash"}

    def model(self, cls=ProtectedReviewModel, route=None, **extra):
        return cls(self.settings, context=self.context, accounting=self.accounting, route_state={} if route is None else route,
            ledger_path=self.store.home / (uuid.uuid4().hex + "-ledger.json"), error_path=self.store.home / "error.json", **extra)

    def test_review_wire_has_only_six_tools_and_no_shell(self):
        model = self.model()
        result = response(arguments={"candidate_id": "candidate-1"})
        with patch("review_model.litellm.completion", return_value=result) as transport:
            answer = model.query([{"role": "user", "content": "Review candidate"}])
        kwargs = transport.call_args.kwargs
        self.assertEqual({t["function"]["name"] for t in kwargs["tools"]}, {"list_files", "read_lines", "search_text", "read_diff", "read_report", "submit_review"})
        self.assertNotIn("bash", canonical_bytes(kwargs["tools"]).decode())
        self.assertEqual(answer["extra"]["actions"][0]["tool"], "read_diff")
        usage = attributed_usage(self.accounting, self.parent["id"])
        self.assertEqual(usage["attempts"], 1)
        self.assertEqual(usage["unknown_cost_attempts"], 1)  # Gateway price is unknown.
        self.assertEqual(self.store.request(self.store_event_request())["state"], "received")

    def store_event_request(self):
        return next(e["data"]["logical_request_id"] for e in self.store.events(self.parent["id"]) if e["kind"] == "logical_request_prepared")

    def test_parent_fallback_is_shared_with_new_and_reused_models(self):
        route = {}
        first, reused = self.model(route=route), self.model(route=route)
        reused.request_ordinal = 1  # Distinct logical identity in this synthetic active role.
        with patch("review_model.litellm.completion", side_effect=[priority_tests.PriorityTests.error(503), response(arguments={"candidate_id": "x"}), response(arguments={"candidate_id": "x"})]) as transport:
            first.query([{"role": "user", "content": "one"}])
            reused.query([{"role": "user", "content": "two"}])
        self.assertEqual([c.kwargs["api_key"] for c in transport.call_args_list], ["primary-test-secret", "backup-test-secret", "backup-test-secret"])
        self.assertEqual(route["provider_index"], 1)
        self.assertEqual(self.model(route=route).provider_index, 1)
        usage = attributed_usage(self.accounting, self.parent["id"])
        self.assertEqual(usage["attempts"], 3)
        self.assertEqual(usage["known_prompt_tokens"], 200)
        self.assertTrue(usage["has_unknown_usage"])

    def test_malformed_response_is_accounted_without_a_transport_retry(self):
        model = self.model()
        with patch("review_model.litellm.completion", return_value=response("bash", {"command": "forbidden"})) as transport:
            with self.assertRaises(FormatError) as stopped:
                model.query([{"role": "user", "content": "Review"}])
        self.assertEqual(transport.call_count, 1)
        self.assertIn("response", stopped.exception.messages[0]["extra"])
        self.assertEqual(self.store.request(self.store_event_request())["state"], "received")
        self.assertEqual(attributed_usage(self.accounting, self.parent["id"])["attempts"], 1)

    def test_baseline_failure_is_visible_even_when_repair_is_accepted(self):
        def report(failures, passed=True):
            return {"task_tests_passed": passed, "protected_tests_unchanged": True,
                "issue_checks": {"tests_run": 10, "passed": 10 if passed else 5, "successful": passed},
                "upstream_targeted": {"tests_run": 4, "passed": 4, "successful": True},
                "full_upstream_suite": {"tests_run": 660, "passed": 660 - len(failures),
                    "successful": not failures, "failures": failures, "errors": []}}
        baseline = report(["timing.test_zero"], False)
        value = json.loads(verification_summary(report(["timing.test_zero"]), baseline))
        self.assertTrue(value["accepted_against_baseline"])
        self.assertFalse(value["full_suite_all_passed"])
        self.assertEqual(value["remaining_failures"]["names"], ["timing.test_zero"])
        self.assertEqual(value["new_failures"]["count"], 0)
        regression = json.loads(verification_summary(report(["timing.test_zero", "split.test_new"]), baseline))
        self.assertFalse(regression["accepted_against_baseline"])
        self.assertEqual(regression["new_failures"]["names"], ["split.test_new"])
        fixed = json.loads(verification_summary(report([]), baseline))
        self.assertTrue(fixed["full_suite_all_passed"])
        self.assertEqual(fixed["remaining_failures"]["count"], 0)

    def test_large_failure_summary_stays_parseable_and_marks_omitted_names(self):
        report = {"task_tests_passed": False, "protected_tests_unchanged": True,
            "issue_checks": {"tests_run": 1, "passed": 0, "successful": False},
            "upstream_targeted": {"tests_run": 1, "passed": 1, "successful": True},
            "full_upstream_suite": {"tests_run": 100, "passed": 0, "successful": False,
                "failures": [("long-\\\"name" * 200) + str(i) for i in range(100)], "errors": []}}
        baseline = report | {"full_upstream_suite": report["full_upstream_suite"] | {"failures": ["old-" + "x" * 300]}}
        text = verification_summary(report, baseline)
        self.assertLessEqual(len(text), 2000)
        value = json.loads(text)
        self.assertEqual(value["new_failures"]["count"], 100)
        self.assertTrue(value["new_failures"]["truncated"])
        self.assertFalse(value["accepted_against_baseline"])

    def test_multiple_tool_rejection_is_saved_and_only_later_valid_call_dispatches(self):
        fixture = StageAFixture()
        c = fixture.snapshot.candidate
        model = self.model()
        invalid = response()
        invalid.choices[0].message.tool_calls *= 2
        report = ReviewReport(review_id="audit-review", run_id=c.run_id, reviewer_role_run_id=fixture.reviewer.role_run_id,
            candidate_id=c.candidate_id, source_tree_digest=c.source_tree_digest, verdict="approve", coverage=("Offline audit check",))
        output = self.store.home / "review-audit.json"
        with patch("review_model.litellm.completion", side_effect=[invalid, response("submit_review", {"report": report.model_dump(mode="json")})]), \
                patch.object(fixture.broker, "dispatch", wraps=fixture.broker.dispatch) as dispatch:
            run_review(model, fixture.broker, SimpleNamespace(check=lambda: None, identity=fixture.reviewer), output, "offline")
        self.assertEqual(dispatch.call_count, 1)
        history = json.loads(output.read_text())["messages"]
        error = next(m for m in history if m.get("extra", {}).get("interrupt_type") == "FormatError")
        self.assertEqual(len(error["extra"]["response"]["choices"][0]["message"]["tool_calls"]), 2)
        self.assertEqual(error["extra"]["review_rejection"]["reason"], "expected_one_tool_call")
        self.assertEqual(error["extra"]["review_rejection"]["call_count"], 2)
        self.assertEqual(attributed_usage(self.accounting, self.parent["id"])["attempts"], 2)

    def test_rejection_preview_is_bounded_with_digest_and_raw_response_retained(self):
        model = self.model()
        invalid = response("unregistered", {"long": "\\\"" * 4000})
        invalid.choices[0].message.tool_calls *= 3
        with patch("review_model.litellm.completion", return_value=invalid), self.assertRaises(FormatError) as stopped:
            model.query([{"role": "user", "content": "Review"}])
        extra = stopped.exception.messages[0]["extra"]
        audit = extra["review_rejection"]
        self.assertLessEqual(len(canonical_bytes(audit)), 2048)
        self.assertEqual(audit["call_count"], 3)
        self.assertTrue(audit["truncated"])
        arguments = invalid.choices[0].message.tool_calls[0].function.arguments
        self.assertEqual(audit["calls"][0]["arguments_sha256"], sha256(arguments.encode()))
        self.assertTrue(audit["calls"][0]["arguments_truncated"])
        self.assertEqual(extra["response"]["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"], arguments)

    def test_credential_in_invalid_response_is_blocked_before_audit_is_saved(self):
        model = self.model()
        invalid = response("bash", {"command": "primary-test-secret"})
        output = self.store.home / "credential-audit.json"
        with patch("review_model.litellm.completion", return_value=invalid), self.assertRaisesRegex(ProtocolError, "credential_in_model_output"):
            run_review(model, SimpleNamespace(), self.context, output, "offline")
        self.assertNotIn("primary-test-secret", output.read_text())
        self.assertNotIn("review_rejection", output.read_text())
        self.assertEqual(attributed_usage(self.accounting, self.parent["id"])["attempts"], 1)

    def test_credentials_in_billed_response_are_not_persisted_as_trajectory(self):
        model = self.model()
        with patch("review_model.litellm.completion", return_value=response(arguments={"candidate_id": "x"}, content="primary-test-secret")):
            with self.assertRaisesRegex(ProtocolError, "credential_in_model_output"):
                model.query([{"role": "user", "content": "Review"}])
        self.assertEqual(attributed_usage(self.accounting, self.parent["id"])["attempts"], 1)
        self.assertNotIn("primary-test-secret", model.ledger_path.read_text())

    def test_cancel_before_request_has_no_ticket(self):
        model = self.model()
        self.store.cancel(self.parent["id"])
        with patch("review_model.litellm.completion") as transport, self.assertRaises(CollaborationStopped):
            model.query([{"role": "user", "content": "Review"}])
        transport.assert_not_called()
        self.assertEqual(attributed_usage(self.accounting, self.parent["id"])["attempts"], 0)

    def test_cancel_after_receipt_preserves_tokens_and_prevents_actions(self):
        model = self.model()
        def cancel_then_return(**kwargs):
            self.store.cancel(self.parent["id"])
            return response(arguments={"candidate_id": "x"})
        with patch("review_model.litellm.completion", side_effect=cancel_then_return) as transport:
            with self.assertRaises(CollaborationStopped):
                model.query([{"role": "user", "content": "Review"}])
        usage = attributed_usage(self.accounting, self.parent["id"])
        self.assertEqual(usage["known_prompt_tokens"], 100)
        self.assertEqual(transport.call_count, 1)

    def test_cancel_on_primary_error_prevents_backup_reservation(self):
        model = self.model()
        def fail(**kwargs):
            self.store.cancel(self.parent["id"])
            raise priority_tests.PriorityTests.error(503)
        with patch("review_model.litellm.completion", side_effect=fail) as transport:
            with self.assertRaises(CollaborationStopped):
                model.query([{"role": "user", "content": "Review"}])
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(attributed_usage(self.accounting, self.parent["id"])["attempts"], 1)

    def test_deadline_reaches_network_timeout_and_prevents_late_request(self):
        self.context.deadline = time.monotonic() + 3
        model = self.model()
        with patch("review_model.litellm.completion", return_value=response(arguments={"candidate_id": "x"})) as transport:
            model.query([{"role": "user", "content": "Review"}])
            self.assertLessEqual(transport.call_args.kwargs["timeout"], 3)
            self.context.deadline = time.monotonic() - 1
            with self.assertRaises(TimeoutError):
                model.query([{"role": "user", "content": "Again"}])
            self.assertEqual(transport.call_count, 1)

    def test_duplicate_logical_identity_never_dispatches_again(self):
        model = self.model()
        with patch("review_model.litellm.completion", return_value=response(arguments={"candidate_id": "x"})) as transport:
            model.query([{"role": "user", "content": "Review"}])
            model.request_ordinal = 0
            with self.assertRaises(ProtocolError):
                model.query([{"role": "user", "content": "Review"}])
            self.assertEqual(transport.call_count, 1)

    def test_reply_projection_keeps_real_tool_ids_without_mutating_raw_history(self):
        model = self.model()
        messages = [{"role": "system", "content": "review"}, {"role": "user", "content": "issue"}]
        for i in range(8):
            call = {"id": "call-" + str(i), "type": "function", "function": {"name": "read_lines", "arguments": "{}"}}
            messages.extend([{"role": "assistant", "content": None, "tool_calls": [call]},
                {"role": "tool", "tool_call_id": call["id"], "content": json.dumps({"content": "x" * 3000, "evidence": {"evidence_id": "ev-" + str(i)}})}])
        before = canonical_bytes(messages)
        projected = model._prepare_messages_for_api(messages)
        self.assertLess(len(canonical_bytes(projected)), len(before))
        self.assertEqual(canonical_bytes(messages), before)
        self.assertEqual([m["tool_call_id"] for m in projected if m["role"] == "tool"], ["call-" + str(i) for i in range(8)])

    def test_actual_reviewer_loop_submits_validated_versioned_report(self):
        fixture = StageAFixture()
        c = fixture.snapshot.candidate
        report = ReviewReport(review_id="runtime-review", run_id=c.run_id, reviewer_role_run_id=fixture.reviewer.role_run_id,
            candidate_id=c.candidate_id, source_tree_digest=c.source_tree_digest, verdict="approve", coverage=("Offline scripted only",))
        class ScriptedReview:
            usage = []
            contains_credentials = staticmethod(lambda text: False)
            def query(self, messages):
                return {"role": "assistant", "extra": {"actions": [{"tool": "submit_review", "arguments": {"report": report.model_dump(mode="json")}, "tool_call_id": "review-1"}]}}
            format_observation_messages = ProtectedReviewModel.format_observation_messages
        context = SimpleNamespace(check=lambda: None, identity=fixture.reviewer)
        result = run_review(ScriptedReview(), fixture.broker, context, fixture.root / "review.json", "offline")
        self.assertEqual(result.review_id, report.review_id)
        self.assertTrue(fixture.broker.reviews)

    def test_repeated_same_evidence_does_not_continue_indefinitely(self):
        fixture = StageAFixture()
        args = {"snapshot_id": fixture.snapshot.candidate.snapshot_id, "document_id": fixture.doc().document_id, "start": 1, "end": 2}
        class Repeated:
            usage = []
            contains_credentials = staticmethod(lambda text: False)
            def query(self, messages):
                return {"role": "assistant", "extra": {"actions": [{"tool": "read_lines", "arguments": args, "tool_call_id": str(len(messages))}]}}
            format_observation_messages = ProtectedReviewModel.format_observation_messages
        with self.assertRaisesRegex(ProtocolError, "review_repeated_evidence_limit"):
            run_review(Repeated(), fixture.broker, SimpleNamespace(check=lambda: None, identity=fixture.reviewer), fixture.root / "repeated.json", "offline")
        self.assertFalse(fixture.broker.reviews)

    def test_repair_continuation_closes_real_submit_without_reexecuting_it(self):
        model = DeterministicToolcallModel(outputs=[make_toolcall_output("first",
            [{"id": "first", "type": "function", "function": {"name": "bash", "arguments": '{"command":"submit"}'}}], [{"command": "submit", "tool_call_id": "first"}]),
            make_toolcall_output("second", [{"id": "second", "type": "function", "function": {"name": "bash", "arguments": '{"command":"submit"}'}}],
            [{"command": "submit", "tool_call_id": "second"}])])
        class Submitting(LocalEnvironment):
            completed_submission = None
            calls = 0
            def execute(self, action, cwd="", **kwargs):
                self.calls += 1
                message = {"role": "exit", "content": "done", "extra": {"exit_status": "Submitted", "submission": "actual completion"}}
                self.completed_submission = {"action": dict(action), "message": message}
                raise Submitted(message)
        env = Submitting()
        agent = DefaultAgent(model, env, system_template="test", instance_template="{{task}}", output_path=self.store.home / "first.json")
        session = RepairSession(agent, self.context)
        self.assertEqual(session.start("original-task")["exit_status"], "Submitted")
        n_calls = agent.n_calls
        (self.store.home / "runs" / self.parent["id"]).mkdir(parents=True)
        session.continue_with_feedback(self.context, "verified-feedback")
        self.assertEqual(env.calls, 2)
        self.assertEqual(agent.n_calls, n_calls + 1)
        self.assertEqual(agent.messages[1]["content"], "original-task")
        receipts = [m for m in agent.messages if m.get("extra", {}).get("completed_submission_receipt")]
        self.assertEqual(receipts[0]["tool_call_id"], "first")

    def test_unproven_submission_cannot_resume(self):
        agent = SimpleNamespace(messages=[{"role": "assistant"}, {"role": "exit", "extra": {"exit_status": "Submitted"}}],
                                env=SimpleNamespace(completed_submission=None))
        with self.assertRaisesRegex(ProtocolError, "cannot_prove"):
            RepairSession(agent, self.context).continue_with_feedback(self.context, "feedback")

    def test_interrupted_live_identity_never_reenters_runner(self):
        parent = self.store.create_collaboration("split-after-boundary", "adaptive_history", "live-offline", {}, execution="live")
        self.store.claim_parent(parent["id"])
        self.store.reconcile_collaborations()
        self.assertEqual(self.store.get(parent["id"])["status"], "interrupted")
        from collaboration_live import run_live_worker
        with patch("collaboration_live.LiveRunner") as runner:
            self.assertEqual(run_live_worker(self.store, parent["id"])["status"], "interrupted")
        runner.assert_not_called()

    def test_real_files_and_grader_complete_feedback_with_scripted_transport(self):
        settings = {k: v for k, v in self.settings.items() if k.startswith("DEEPSEEK")}
        settings["DEEPSEEK_API_KEY"] = "offline-unique-" + uuid.uuid4().hex
        parent = self.store.create_collaboration("split-after-boundary", "adaptive_history", "offline-full-wire",
            {"parent_seconds": 120, "role_seconds": {p: 50 for p in ("repairing", "reviewing", "revising", "final_review")}}, execution="live")
        runner = LiveRunner(self.store, parent["id"], settings=settings, accounting=self.accounting)
        runner.setup()
        counts = {}
        def scripted_transport(**kwargs):
            role = next(r for r in self.store.roles(parent["id"]) if r["status"] == "running")
            phase = role["phase"]
            counts[phase] = counts.get(phase, 0) + 1
            if role["role"] == "repairer":
                if counts[phase] == 2:
                    return response("bash", {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})
                old = "            if maxsplit == 1:\n                yield list(it)\n"
                partial = "            if maxsplit == 1:\n                rest = list(it)\n                if rest and any(rest):\n                    yield rest\n"
                fixed = "            if maxsplit == 1:\n                rest = list(it)\n                if rest:\n                    yield rest\n"
                code = "from pathlib import Path; p=Path('more_itertools/more.py'); s=p.read_text(); p.write_text(s.replace(" + repr(old if phase == "repairing" else partial) + "," + repr(partial if phase == "repairing" else fixed) + "))"
                if phase == "revising":
                    code += "; import json; Path('temp/finding_responses.json').write_text(json.dumps([{'finding_id':'zero-remainder','disposition':'accepted','reason':'Scripted fixture correction; independently checked afterwards','evidence_ids':[]}]))"
                return response("bash", {"command": "python -B -c " + shlex.quote(code)})
            initial = json.loads(kwargs["messages"][1]["content"].split("\n", 1)[1])
            if phase == "reviewing" and counts[phase] == 1:
                doc = next(d for d in initial["documents"] if d["side"] == "candidate" and d["path"] == "more_itertools/more.py")
                return response("read_lines", {"snapshot_id": initial["snapshot_id"], "document_id": doc["document_id"], "start": 1427, "end": 1433})
            report = {k: initial[k] for k in ("run_id", "reviewer_role_run_id", "candidate_id", "source_tree_digest", "review_id")}
            report.update(verdict="request_changes" if phase == "reviewing" else "approve", coverage=["Scripted review, not a real model"], findings=[])
            if phase == "reviewing":
                ev = json.loads(kwargs["messages"][-1]["content"])["evidence"]["evidence_id"]
                report["findings"] = [{"finding_id": "zero-remainder", "severity": "major", "category": "behavior",
                    "claim": "A nonempty zero remainder must be preserved.", "evidence_ids": [ev], "requirement_ref": initial["requirements"][0]["requirement_id"],
                    "impact": "Scripted edge-case concern; independent tests assess code."}]
            return response("submit_review", {"report": report})
        with patch("litellm.completion", side_effect=scripted_transport):
            result = CollaborationCoordinator(self.store, runner).run(parent["id"])
        self.assertEqual(result["status"], "finished", result)
        self.assertEqual(counts, {"repairing": 2, "reviewing": 2, "revising": 2, "final_review": 1})
        runner.finalize(result)
        runner.close()
        final = self.store.get(parent["id"])
        self.assertEqual(final["repair_result"], "passed")
        self.assertEqual(final["review_verdict"], "approve")
        self.assertEqual(len(self.store.records(parent["id"], "response")), 1)
        self.assertEqual(attributed_usage(self.accounting, parent["id"])["attempts"], 7)
        self.assertTrue((runner.output / "patch-replay/report.json").exists())

    def test_owned_grader_timeout_stops_process_and_starts_no_model(self):
        parent = self.store.create_collaboration("split-after-boundary", "adaptive_history", "timeout-command", {}, execution="live")
        runner = LiveRunner(self.store, parent["id"], settings=self.settings, accounting=self.accounting)
        with self.assertRaises(TimeoutError):
            runner.command([sys.executable, "-B", "-c", "import time; time.sleep(60)"], timeout=.1, check=lambda: None)
        self.assertEqual(attributed_usage(self.accounting, parent["id"])["attempts"], 0)

    def test_review_provider_failure_preserves_accepted_patch_without_more_roles(self):
        settings = {k: v for k, v in self.settings.items() if k.startswith("DEEPSEEK")}
        settings["DEEPSEEK_API_KEY"] = "offline-unique-" + uuid.uuid4().hex
        parent = self.store.create_collaboration("split-after-boundary", "adaptive_history", "offline-review-outage", {}, execution="live")
        runner = LiveRunner(self.store, parent["id"], settings=settings, accounting=self.accounting)
        runner.setup()
        phases = []
        def transport(**kwargs):
            role = next(r for r in self.store.roles(parent["id"]) if r["status"] == "running")
            phases.append(role["phase"])
            if role["phase"] == "reviewing":
                raise priority_tests.PriorityTests.error(503)
            if len(phases) == 2:
                return response("bash", {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})
            old = "            if maxsplit == 1:\n                yield list(it)\n"
            fixed = "            if maxsplit == 1:\n                rest = list(it)\n                if rest:\n                    yield rest\n"
            code = "from pathlib import Path; p=Path('more_itertools/more.py'); p.write_text(p.read_text().replace(" + repr(old) + "," + repr(fixed) + "))"
            return response("bash", {"command": "python -B -c " + shlex.quote(code)})
        with patch("litellm.completion", side_effect=transport):
            result = CollaborationCoordinator(self.store, runner).run(parent["id"])
        self.assertEqual(result["status"], "failed")
        runner.finalize(result)
        runner.close()
        final = self.store.get(parent["id"])
        self.assertEqual(final["status"], "failed")  # Terminal status is never revived.
        self.assertEqual(final["repair_result"], "passed")
        self.assertEqual(final["collaboration_result"], "degraded")
        self.assertEqual(final["review_verdict"], "unavailable")
        self.assertEqual(phases, ["repairing", "repairing", "reviewing"])
        self.assertEqual([r["status"] for r in self.store.roles(parent["id"])], ["succeeded", "failed", "skipped", "skipped"])
        usage = attributed_usage(self.accounting, parent["id"])
        self.assertEqual(usage["attempts"], 3)
        self.assertEqual(usage["unknown_cost_attempts"], 1)

    def test_repair_projection_reserves_card_appended_after_context_compaction(self):
        model = self.model(cls=RepairModel, context_policy=policy("adaptive_history")["context_budget"],
            task_state=SimpleNamespace(pinned_message=lambda: {"role": "user", "content": "facts" * 50}))
        messages = [{"role": "system", "content": "repair"}, {"role": "user", "content": "issue" * 1950}]
        for i in range(4):
            call = {"id": f"c-{i}", "type": "function", "function": {"name": "bash", "arguments": "{}"}}
            messages.extend([{"role": "assistant", "content": None, "tool_calls": [call]},
                {"role": "tool", "tool_call_id": call["id"], "content": "source" * 220,
                 "extra": {"returncode": 0, "tool_output": {"sequence": i, "raw_bytes": 1320,
                    "full_output_path": f"temp/tool-output/{i:04d}.txt"}}}])
        from context_budget import message_bytes
        card = {"role": "user", "content": "当前任务卡" * 210}
        before = canonical_bytes(messages)
        without_card = model._prepare_messages_for_api(messages)
        self.assertLessEqual(message_bytes(without_card), 16384)
        legacy = without_card + [card]
        self.assertGreater(message_bytes(legacy), 16384)
        model._task_card_receipt = {"message": card}
        corrected = model._prepare_messages_for_api(messages)
        self.assertLessEqual(message_bytes(corrected), 16384)
        self.assertEqual(corrected[-1], card)
        self.assertEqual(corrected.count(card), 1)
        self.assertEqual(before, canonical_bytes(messages))

    def test_live_metadata_cannot_resurrect_or_rewrite_terminal_status(self):
        parent = self.store.create_collaboration("split-after-boundary", "adaptive_history", "immutable-live", {}, execution="live")
        self.store.stop(parent["id"], "failed", "offline failure")
        with self.assertRaises(ProtocolError):
            self.store.attach_live_result(parent["id"], "receipts", {"status": "running"})
        fields = {"metrics": {"attempts": 0}, "artifacts": {}}
        self.store.attach_live_result(parent["id"], "receipts", fields)
        with self.assertRaises(ProtocolError):
            self.store.attach_live_result(parent["id"], "receipts", fields | {"metrics": {"attempts": 1}})
        self.assertEqual(self.store.get(parent["id"])["status"], "failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
