"""Source workflow contracts and local model/check substitutes; zero real model calls."""
import copy
import io
import json
import os
from pathlib import Path
import time
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch, Mock
import uuid
import zipfile
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from analysis_runtime import AnalysisBroker, create_analysis, run_analysis, ReadSource
from analysis_sources import import_source
from collaboration_panel import registered_artifact
from collaboration_protocol import canonical_bytes, sha256
from deepseek_budget import SharedBudget, load_budget_config
from source_repair import create_source_repair, RepairWorkspace
from source_repair_execution import execute_source_repair
from reference_sources import public_url, read_public_page, excerpt
from task_service_store import ROOT
import test_analysis as base

RUNTIME = {'python': {'image_id': 'sha256:' + 'a' * 64, 'tag': 'python:3.10-slim', 'repo_digests': ['python@sha256:' + 'b' * 64],
                      'test_tools': {'sha256': 'c' * 64, 'versions': {'pytest': 'fixture'}, 'files': 0, 'bytes': 0}}}
SETTINGS = {'BUPT_API_KEY': 'primary-test-secret', 'BUPT_API_BASE_URL': 'https://myai.bupt.edu.cn/llm-gw/v1', 'BUPT_CHAT_MODEL': 'deepseek-v4-flash',
            'DEEPSEEK_API_KEY': 'backup-test-secret', 'DEEPSEEK_API_BASE_URL': 'https://api.deepseek.com', 'DEEPSEEK_CHAT_MODEL': 'deepseek-flash'}
ACCEPTANCE = "import unittest\nfrom app import divide\nclass GoalTests(unittest.TestCase):\n    def test_zero(self):\n        with self.assertRaises(ValueError): divide(1, 0)\n    def test_normal(self):\n        self.assertEqual(divide(6, 2), 3)\n"


class WorkflowTests(unittest.TestCase):
    setUp = base.AnalysisTests.setUp
    body = base.AnalysisTests.body

    def analysis(self, *, references='off', original_tests=False, findings=1):
        if original_tests:
            self.source = import_source(self.store.home, 'code.zip', base.zipped([
                ('app.py', self.raw), ('test_existing.py', b'import unittest\nclass Old(unittest.TestCase):\n    def test_old(self): self.assertTrue(True)\n')]))
        row = create_analysis(self.store, self.body(paths=sorted(self.source['files']), reference_mode=references))
        broker = AnalysisBroker(self.store.home, row)
        ref = broker.span('app.py', 1, 2)
        report = base.report(ref['evidence_id'])
        report['findings'] = [copy.deepcopy(report['findings'][0]) for _ in range(findings)]
        broker.dispatch('submit_analysis', report)
        folder = self.store.home / 'runs' / row['id']; folder.mkdir(parents=True)
        data = canonical_bytes(broker.report)
        (folder / 'report.json').write_bytes(data)
        (folder / 'artifact_sha256.json').write_bytes(canonical_bytes({'report.json': sha256(data)}))
        self.store.update(row['id'], status='running')
        return self.store.update(row['id'], status='finished', artifacts={'report.json': str(folder / 'report.json')})

    def repair(self, parent=None):
        parent = parent or self.analysis()
        body = {'analysis_id': parent['id'], 'goal': '零分母应明确抛出ValueError；正常除法保持正确。',
                'request_key': uuid.uuid4().hex, 'execution_ack': True, 'external_processing_ack': True}
        with patch('source_checks.require_runtime', return_value=copy.deepcopy(RUNTIME)):
            return create_source_repair(self.store, body)

    def test_panel_supervisor_dispatches_source_repair_once(self):
        from task_service import Supervisor
        row = self.repair()
        supervisor = Supervisor(self.store)
        try:
            with patch('task_service.subprocess.Popen') as launch:
                launch.return_value.poll.return_value = None
                supervisor.tick()
                supervisor.tick()
                self.assertEqual(launch.call_count, 1)
                command = launch.call_args.args[0]
                self.assertIn('--source-repair', command)
                self.assertIn(row['id'], command)
                self.assertNotIn('--analysis', command)
        finally:
            if supervisor.log is not None:
                supervisor.log.close()

    def execute(self, row, *, bad_checks=False, review_missing=False, no_edits=False, callback=None, cancel_after=None):
        path = self.store.home / 'synthetic_shared.json'
        path.write_bytes(canonical_bytes({'historical_estimated_cost_cny': '0', 'historical_unknown_charge_reservation_cny': '0', 'blocked': False, 'requests': {}}))
        budget = SharedBudget(load_budget_config() | {'reservation_per_attempt_cny': '0.20'}, ledger_path=path, temp_dir=self.store.home / 'locks')
        calls, check_inputs = [], []
        author_counts = {}
        def complete(**kwargs):
            calls.append(kwargs)
            if callback:
                value = callback(kwargs)
                if value is not None: return value
            names = {tool['function']['name'] for tool in kwargs['tools']}
            data = json.loads(kwargs['messages'][1]['content'])
            if 'submit_acceptance' in names:
                return base.tool_response('submit_acceptance', {'criteria': ['零分母给明确ValueError。', '正常除法结果不变。'],
                    'tests': [{'path': 'acceptance_division.py', 'content': ACCEPTANCE}], 'limitations': []})
            if 'submit_changes' in names:
                number = data['round']; author_counts[number] = author_counts.get(number, 0) + 1
                if no_edits or author_counts[number] > 1:
                    return base.tool_response('submit_changes', {'summary': '离线作者回执，不代表模型能力成绩。', 'remaining': []})
                if number == 1:
                    return base.tool_response('edit_source', {'path': 'app.py', 'old': 'return a / b', 'new': 'return 0 if b == 0 else a / b'})
                return base.tool_response('edit_source', {'path': 'app.py', 'old': 'return 0 if b == 0 else a / b',
                    'new': "if b == 0:\n        raise ValueError('zero denominator')\n    return a / b"})
            if review_missing: return base.tool_response('read_source', {'path': 'app.py', 'start': 1, 'end': 2})
            correct = any('raise ValueError' in file['content'] for file in data['current_code']['files'])
            return base.tool_response('submit_repair_review', {'verdict': 'satisfied' if correct else 'needs_work',
                'reason': '离线复核回执：按当前代码和实际传入的检查记录判定。',
                'blockers': [] if correct else ['零分母应抛出明确异常，不能返回0。'], 'unknowns': []})
        def checks(files, acceptance, runtime, *, output, deadline, check):
            check()
            check_inputs.append((dict(files), dict(acceptance)))
            if cancel_after and len(check_inputs) == cancel_after: self.store.cancel(row['id']); check()
            ok = b'raise ValueError' in files['app.py'] and not bad_checks
            return {'passed': ok, 'tree_sha256': sha256(canonical_bytes({path: sha256(raw) for path, raw in sorted(files.items())})),
                'checks': [{'language': 'python', 'actual_execution': True, 'runner': 'offline_check_substitute', 'passed': ok,
                            'exit_code': 0 if ok else 1, 'report': {'tests': 2, 'failures': 0 if ok else 1, 'errors': 0, 'skipped': 0},
                            'output_tail': 'OFFLINE substitute: expected ValueError', 'log': 'synthetic'}]}
        with patch('dotenv.dotenv_values', return_value=SETTINGS), patch('task_service_worker.RunBudget', return_value=budget), \
             patch('review_model.litellm.completion', side_effect=complete), patch('source_repair_execution.require_runtime', return_value=copy.deepcopy(RUNTIME)):
            execute_source_repair(self.store, row['id'], check_runner=checks)
        return self.store.get(row['id']), calls, check_inputs, budget

    def report(self, row): return json.loads(registered_artifact(self.store, row, 'report.json'))

    def test_whole_goal_two_round_loop_preserves_original_and_frozen_acceptance(self):
        row, calls, inputs, budget = self.execute(self.repair())
        self.assertEqual(row['status'], 'finished')
        self.assertEqual(row['repair_result'], 'offline_flow_completed')
        report = self.report(row)
        self.assertEqual(report['validation_scope'], 'offline_substitute')
        self.assertEqual(len(report['rounds']), 2)
        self.assertFalse(report['rounds'][0]['checks']['passed'])
        self.assertTrue(report['rounds'][1]['checks']['passed'])
        self.assertEqual([item['review']['verdict'] for item in report['rounds']], ['needs_work', 'satisfied'])
        self.assertTrue(all(item[1] == inputs[0][1] for item in inputs))
        self.assertEqual((self.store.home / 'sources' / self.source['id'] / 'files/app.py').read_bytes(), self.raw)
        self.assertEqual(len(calls), len(json.loads(budget.path.read_bytes())['requests']))
        archive = registered_artifact(self.store, row, 'candidate-02.zip')
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            self.assertIn(b'raise ValueError', zipped.read('app.py'))
            self.assertNotIn('acceptance_division.py', zipped.namelist())
        self.assertTrue(all(not any(message['role'] == 'assistant' for message in call['messages']) for call in calls))

    def test_no_progress_stops_at_declared_round_cap_without_claiming_success(self):
        row, calls, inputs, _ = self.execute(self.repair(), no_edits=True)
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['repair_result'], 'unverified')
        self.assertEqual(len(self.report(row)['rounds']), 2)
        self.assertIn('连续两轮', row['reason'])
        self.assertEqual(len(calls), 5)

    def test_stagnant_author_yields_changed_candidate_to_checks_without_fabricated_submission(self):
        count = 0
        def write_then_stall(kwargs):
            nonlocal count
            if 'submit_changes' not in {tool['function']['name'] for tool in kwargs['tools']}: return None
            count += 1
            if count == 1:
                return base.tool_response('edit_source', {'path': 'app.py', 'old': 'return a / b',
                    'new': "if b == 0:\n        raise ValueError('zero denominator')\n    return a / b"})
            return base.tool_response('read_source', {'path': 'app.py', 'start': 1, 'end': 4})
        ended, _, inputs, _ = self.execute(self.repair(), callback=write_then_stall)
        report = self.report(ended)
        self.assertEqual(ended['repair_result'], 'offline_flow_completed')
        self.assertEqual(count, 9)
        self.assertEqual(len(inputs), 2)
        self.assertIsNone(report['rounds'][0]['author'])
        self.assertEqual(report['rounds'][0]['author_execution']['status'], 'yielded_to_checks')
        self.assertTrue(report['rounds'][0]['checks']['passed'])
        self.assertEqual(report['rounds'][0]['review']['verdict'], 'satisfied')

    def test_author_handoff_cannot_override_failed_checks_even_with_positive_review(self):
        count = 0
        def write_then_stall(kwargs):
            nonlocal count
            if 'submit_changes' not in {tool['function']['name'] for tool in kwargs['tools']}: return None
            data = json.loads(kwargs['messages'][1]['content'])
            if data['round'] > 1: return base.tool_response('submit_changes', {'summary': 'No additional offline changes.'})
            count += 1
            if count == 1:
                return base.tool_response('edit_source', {'path': 'app.py', 'old': 'return a / b',
                    'new': "if b == 0:\n        raise ValueError('zero denominator')\n    return a / b"})
            return base.tool_response('read_source', {'path': 'app.py', 'start': 1, 'end': 4})
        ended, _, _, _ = self.execute(self.repair(), callback=write_then_stall, bad_checks=True)
        report = self.report(ended)
        self.assertEqual(ended['status'], 'failed')
        self.assertEqual(report['rounds'][0]['author_execution']['status'], 'yielded_to_checks')
        self.assertTrue(all(not item['checks']['passed'] for item in report['rounds']))
        self.assertTrue(all(item['review']['verdict'] == 'satisfied' for item in report['rounds']))

    def test_repeated_code_versions_stop_instead_of_resetting_progress(self):
        def alternate(kwargs):
            if 'submit_changes' not in {tool['function']['name'] for tool in kwargs['tools']}: return None
            data = json.loads(kwargs['messages'][1]['content'])
            text = next(item['content'] for item in data['current_code']['files'] if item['path'] == 'app.py')
            before, after = ('return 42', 'return a / b') if 'return 42' in text else ('return a / b', 'return 42')
            return base.tool_response('edit_source', {'path': 'app.py', 'old': before, 'new': after})
        ended, calls, inputs, _ = self.execute(self.repair(), callback=alternate)
        self.assertEqual(ended['status'], 'failed')
        self.assertIn('连续8次', ended['reason'])
        self.assertEqual(len(inputs), 1)  # Baseline only; no pretend finished round.
        self.assertLess(len(calls), 15)

    def test_edited_long_file_and_previous_action_remain_in_next_request(self):
        self.raw += ''.join(f'# current source line {number:03d} ' + 'x' * 25 + '\n' for number in range(1, 181)).encode()
        self.source = import_source(self.store.home, 'app.py', self.raw)
        row, seen, count = self.repair(), [], 0
        def inspect(kwargs):
            nonlocal count
            if 'submit_changes' not in {tool['function']['name'] for tool in kwargs['tools']}: return None
            data = json.loads(kwargs['messages'][1]['content']); count += 1
            if count <= 2:
                return base.tool_response('read_source', {'path': 'app.py', 'start': 1 if count == 1 else 101,
                                                        'end': 100 if count == 1 else 182})
            if count == 3:
                return base.tool_response('edit_source', {'path': 'app.py', 'old': 'return a / b', 'new': 'return 42'})
            seen.append(data)
            self.store.cancel(row['id'])
            return base.tool_response('submit_changes', {'summary': 'Offline cancellation after observing the updated request.'})
        ended, _, _, _ = self.execute(row, callback=inspect)
        self.assertEqual(ended['status'], 'cancelled')
        data = seen[0]
        body = next(item['content'] for item in data['current_code']['read_ranges'] if item['path'] == 'app.py')
        self.assertEqual(body, self.raw.decode().replace('\r\n', '\n').replace('return a / b', 'return 42').rstrip('\n'))
        action = data['recent_actions'][-1]['actions'][0]
        self.assertEqual(action['tool'], 'edit_source')
        self.assertEqual(action['arguments']['new'], 'return 42')
        self.assertEqual(action['result']['file_sha256'], sha256(self.raw.decode('utf-8-sig').replace('return a / b', 'return 42').encode()))

    def test_long_source_reads_survive_another_action_in_real_phase_messages(self):
        long_source = ''.join(f'# retained source line {number:03d} ' + 'x' * 25 + '\n' for number in range(1, 181))
        self.source = import_source(self.store.home, 'code.zip', base.zipped([
            ('app.py', self.raw), ('large.py', long_source.encode())]))
        count, inspected = 0, []
        def inspect(kwargs):
            nonlocal count
            names = {tool['function']['name'] for tool in kwargs['tools']}
            if 'submit_acceptance' not in names: return None
            data = json.loads(kwargs['messages'][1]['content'])
            count += 1
            if count <= 2:
                return base.tool_response('read_source', {'path': 'large.py', 'start': 1 if count == 1 else 101,
                                                         'end': 100 if count == 1 else 180})
            if count == 3:
                return base.tool_response('read_analysis_finding', {'finding_id': 'F001'})
            inspected.append((data, names))
            return None
        ended, _, _, _ = self.execute(self.repair(), callback=inspect)
        self.assertEqual(count, 4)
        self.assertEqual(ended['status'], 'finished', ended['reason'])
        data, names = inspected[0]
        ranges = [item for item in data['current_code']['read_ranges'] if item['path'] == 'large.py']
        self.assertEqual(len(ranges), 1)
        self.assertEqual(ranges[0]['content'], long_source.rstrip('\n'))
        self.assertNotIn('read_source', names)

    def test_source_read_memory_invalidates_changed_file_and_counts_actual_new_lines(self):
        from source_read_memory import SourceReadMemory
        row = self.repair(); output = self.store.home / 'read-memory'; output.mkdir()
        workspace = RepairWorkspace(self.store.home, row, output)
        memory = SourceReadMemory(workspace)
        memory.remember(workspace.read('app.py', 1, 2))
        first = memory.signature()
        memory.remember(workspace.read('app.py', 1, 1))
        self.assertEqual(first, memory.signature())
        self.assertEqual(memory.material(workspace.material(0))['missing_ranges'], [])
        workspace.edit('edit_source', {'path': 'app.py', 'old': 'return a / b', 'new': 'return 42'})
        updated = memory.material(workspace.material(0))
        self.assertEqual(updated['read_ranges'], [])
        self.assertEqual(updated['missing_ranges'], [{'path': 'app.py', 'start': 1, 'end': 2}])
        memory.remember(workspace.read('app.py', 1, 2))
        self.assertIn('return 42', memory.material(workspace.material(0))['read_ranges'][0]['content'])

    def test_large_source_scope_preserves_payload_and_requires_matching_accounting(self):
        from source_flow_model import SourceFlowModel
        from source_repair import SubmitChanges
        from minisweagent.exceptions import LimitsExceeded
        config = load_budget_config()
        path = self.store.home / 'large-scope-budget.json'
        path.write_bytes(canonical_bytes({'historical_estimated_cost_cny': '0', 'historical_unknown_charge_reservation_cny': '0', 'blocked': False, 'requests': {}}))
        small = SharedBudget(config, ledger_path=path, temp_dir=self.store.home / 'scope-locks')
        large = SharedBudget(config | {'reservation_per_attempt_cny': '0.20'}, ledger_path=path, temp_dir=self.store.home / 'scope-locks')
        kwargs = dict(schemas={'submit_changes': SubmitChanges}, descriptions={'submit_changes': 'Submit'},
                      audit={'path': self.store.home / 'scope-requests.jsonl'}, model_role='repairer',
                      request_deadline=lambda: time.monotonic() + 60,
                      ledger_path=self.store.home / 'scope-ledger.json', error_path=self.store.home / 'scope-error.json')
        settings = {key: value for key, value in SETTINGS.items() if key.startswith('DEEPSEEK_')}
        with self.assertRaisesRegex(ValueError, 'shared ledger reservation'):
            SourceFlowModel(settings, budget=small, large_source_context=True, **kwargs)
        model = SourceFlowModel(settings, budget=large, large_source_context=True, **kwargs)
        messages = [{'role': 'user', 'content': 'actual selected source\n' * 2000}]
        with patch('review_model.litellm.completion', return_value=base.tool_response('submit_changes', {'summary': 'Offline scope check.'})) as transport:
            model.query(messages)
            self.assertEqual(transport.call_args.kwargs['messages'], messages)
            transport.reset_mock()
            with self.assertRaises(LimitsExceeded): model.query([{'role': 'user', 'content': 'x' * 66000}])
            transport.assert_not_called()
        self.assertEqual(config['max_messages_utf8_bytes'], 16384)
        self.assertEqual(config['reservation_per_attempt_cny'], '0.10')
        self.assertEqual(model.budget_config['max_messages_utf8_bytes'], 65536)

    def test_invalid_read_batch_keeps_response_usage_and_allows_corrected_reply(self):
        from source_flow_model import SourceFlowModel
        from source_repair import SubmitChanges
        from minisweagent.exceptions import FormatError
        path = self.store.home / 'bad-batch-budget.json'
        path.write_bytes(canonical_bytes({'historical_estimated_cost_cny': '0', 'historical_unknown_charge_reservation_cny': '0', 'blocked': False, 'requests': {}}))
        budget = SharedBudget(load_budget_config(), ledger_path=path, temp_dir=self.store.home / 'batch-locks')
        model = SourceFlowModel({key: value for key, value in SETTINGS.items() if key.startswith('DEEPSEEK_')},
            schemas={'read_source': ReadSource, 'submit_changes': SubmitChanges},
            descriptions={'read_source': 'Read', 'submit_changes': 'Submit'},
            audit={'path': self.store.home / 'bad-batch-requests.jsonl'}, budget=budget,
            ledger_path=self.store.home / 'bad-batch-ledger.json', error_path=self.store.home / 'bad-batch-error.json',
            request_deadline=lambda: time.monotonic() + 60)
        bad = base.tool_response('read_source', {'path': 'app.py', 'start': 1, 'end': 2})
        bad.choices[0].message.tool_calls.append(copy.deepcopy(bad.choices[0].message.tool_calls[0]))
        with patch('review_model.litellm.completion', side_effect=[bad, base.tool_response('submit_changes', {'summary': 'Corrected reply.'})]):
            with self.assertRaises(FormatError) as caught: model.query([{'role': 'user', 'content': 'Inspect current code.'}])
            self.assertIn('response', caught.exception.messages[0]['extra'])
            self.assertIn('cost', caught.exception.messages[0]['extra'])
            self.assertEqual(model.query([{'role': 'user', 'content': 'Correct the duplicate call IDs.'}])['extra']['actions'][0]['tool'], 'submit_changes')
        self.assertEqual(model.requests, 2)
        self.assertTrue(all(row['status'] == 'settled' for row in json.loads(path.read_bytes())['requests'].values()))

    def test_full_analysis_reasoning_and_verdict_stay_visible_across_source_edits(self):
        asked, observed = set(), []
        def read_finding(kwargs):
            names = {tool['function']['name'] for tool in kwargs['tools']}
            data = json.loads(kwargs['messages'][1]['content'])
            key = (data['round'], 'author' if 'submit_changes' in names else 'reviewer')
            if key not in asked:
                asked.add(key)
                self.assertEqual(data['candidate_issue_index'][0]['review_verdict'], 'not_reviewed')
                return base.tool_response('read_analysis_finding', {'finding_id': 'F001'})
            finding = data['analysis_finding']
            self.assertIn('直接执行', finding['finding']['reasoning'])
            self.assertEqual(finding['original_locations'][0]['path'], 'app.py')
            self.assertEqual(finding['original_locations'][0]['file_sha256'], sha256(self.raw))
            observed.append((key, data['latest_observation']))
            return None
        ended, _, _, _ = self.execute(self.repair(), callback=read_finding)
        self.assertEqual(ended['status'], 'finished', ended['reason'])
        self.assertTrue(any(value and 'tree_sha256' in value for _, value in observed))
        self.assertEqual(len(asked), 5)  # Planning and both roles in two rounds.

    def test_rejected_analysis_is_not_promoted_and_oversize_reasoning_is_not_silently_cut(self):
        from source_repair_handoff import AnalysisHandoff
        parent = self.analysis()
        analysis = self.report(parent)
        analysis['findings'][0]['review'] = {'verdict': 'rejected', 'category': 'improvement', 'reason': '调用合同允许抛异常。'}
        row = self.repair(parent); output = self.store.home / 'handoff'; output.mkdir()
        handoff = AnalysisHandoff(analysis, RepairWorkspace(self.store.home, row, output))
        self.assertEqual(handoff.index()[0]['review_verdict'], 'rejected')
        self.assertEqual(handoff.read('F001')['finding']['review']['reason'], '调用合同允许抛异常。')
        with self.assertRaises(ValueError): handoff.read('F999')
        analysis['findings'][0]['reasoning'] = '完整理由' * 2500
        with self.assertRaisesRegex(ValueError, '未截断'): handoff.read('F001')

    def test_green_model_review_cannot_override_failed_program_checks(self):
        row, calls, inputs, _ = self.execute(self.repair(), bad_checks=True)
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['repair_result'], 'unverified')
        self.assertTrue(any(item['review']['verdict'] == 'satisfied' for item in self.report(row)['rounds']))

    def test_verifier_receives_frozen_test_bodies_and_preserves_nonblocking_limitations(self):
        seen = []
        def verify(kwargs):
            if 'submit_repair_review' not in {tool['function']['name'] for tool in kwargs['tools']}: return None
            data = json.loads(kwargs['messages'][1]['content'])
            self.assertEqual(data['frozen_acceptance']['tests'][0]['content'], ACCEPTANCE)
            self.assertEqual(data['frozen_acceptance']['tests_sha256'], sha256(canonical_bytes({'acceptance_division.py': ACCEPTANCE})))
            seen.append(data['frozen_acceptance'])
            if data['program_checks']['passed']:
                return base.tool_response('submit_repair_review', {'verdict': 'satisfied', 'reason': '依据当前源码、冻结测试正文及本轮实际结果。',
                    'blockers': [], 'unknowns': [], 'limitations': ['只验证已声明的隔离环境，不证明所有部署环境。']})
        ended, _, _, _ = self.execute(self.repair(), callback=verify)
        self.assertEqual(ended['status'], 'finished', ended['reason'])
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])
        self.assertTrue(self.report(ended)['rounds'][-1]['review']['limitations'])

    def test_material_uncertainty_still_blocks_satisfied_verdict(self):
        def uncertain(kwargs):
            if 'submit_repair_review' not in {tool['function']['name'] for tool in kwargs['tools']}: return None
            return base.tool_response('submit_repair_review', {'verdict': 'satisfied', 'reason': '仍缺少目标必需的依据。',
                'blockers': [], 'unknowns': ['无法确认目标要求的关键边界行为。'], 'limitations': []})
        ended, _, _, _ = self.execute(self.repair(), callback=uncertain)
        self.assertEqual(ended['status'], 'failed')
        self.assertEqual(ended['repair_result'], 'unverified')

    def test_missing_reviewer_submission_is_not_silently_approved(self):
        row, calls, inputs, _ = self.execute(self.repair(), review_missing=True)
        self.assertEqual(row['status'], 'failed')
        self.assertTrue(all(item['review'] is None for item in self.report(row)['rounds']))
        self.assertEqual(len(self.report(row)['rounds']), 1)
        self.assertIn('连续8次', row['reason'])

    def test_parallel_finding_reads_are_serially_dispatched_and_both_stay_visible(self):
        asked, seen = set(), []
        def read_both(kwargs):
            data = json.loads(kwargs['messages'][1]['content'])
            names = {tool['function']['name'] for tool in kwargs['tools']}
            key = (data['round'], 'author' if 'submit_changes' in names else 'reviewer')
            self.assertNotIn('read_source', names)
            if key not in asked:
                asked.add(key)
                read_tool = next(tool['function'] for tool in kwargs['tools'] if tool['function']['name'] == 'read_analysis_finding')
                self.assertEqual(read_tool['parameters']['properties']['finding_id']['enum'], ['F001', 'F002'])
                one = base.tool_response('read_analysis_finding', {'finding_id': 'F001'})
                two = base.tool_response('read_analysis_finding', {'finding_id': 'F002'})
                one.choices[0].message.tool_calls.extend(two.choices[0].message.tool_calls)
                return one
            materials = [data['analysis_finding'], *data['additional_analysis_findings']]
            self.assertNotIn('read_analysis_finding', names)
            self.assertEqual({item['finding']['finding_id'] for item in materials}, {'F001', 'F002'})
            self.assertTrue(all(item['original_locations'] and item['finding']['reasoning'] for item in materials))
            seen.append(key)
        ended, _, _, _ = self.execute(self.repair(self.analysis(findings=2)), callback=read_both)
        self.assertEqual(ended['status'], 'finished', ended['reason'])
        self.assertEqual(set(seen), asked)

    def test_mixed_read_and_edit_batch_executes_neither_and_keeps_exact_correction(self):
        attempted, checked = False, False
        def mixed(kwargs):
            nonlocal attempted, checked
            if 'submit_changes' not in {tool['function']['name'] for tool in kwargs['tools']}: return None
            data = json.loads(kwargs['messages'][1]['content'])
            if not attempted:
                attempted = True
                one = base.tool_response('read_analysis_finding', {'finding_id': 'F001'})
                two = base.tool_response('edit_source', {'path': 'app.py', 'old': 'return a / b', 'new': 'return 42'})
                one.choices[0].message.tool_calls.extend(two.choices[0].message.tool_calls)
                return one
            if not checked:
                checked = True
                self.assertIsNone(data['analysis_finding'])
                self.assertIn('return a / b', data['current_code']['files'][0]['content'])
                self.assertIn('expected_one_tool_call', kwargs['messages'][-1]['content'])
        ended, _, _, _ = self.execute(self.repair(), callback=mixed)
        self.assertTrue(checked)
        self.assertEqual(ended['status'], 'finished', ended['reason'])

    def test_new_source_evidence_can_progress_beyond_old_five_request_planning_cap(self):
        self.raw += b'# context\n' * 8
        self.source = import_source(self.store.home, 'app.py', self.raw)
        count = 0
        def reads(kwargs):
            nonlocal count
            if 'submit_acceptance' not in {tool['function']['name'] for tool in kwargs['tools']}: return None
            count += 1
            if count <= 7:
                return base.tool_response('read_source', {'path': 'app.py', 'start': count, 'end': count})
        ended, _, _, _ = self.execute(self.repair(), callback=reads)
        self.assertEqual(count, 8)
        self.assertEqual(ended['status'], 'finished', ended['reason'])

    def test_source_receipt_deduplicates_only_exact_visible_body_and_current_hash(self):
        from source_repair_execution import source_observation
        body = 'first\nsecond\n'
        material = {'files': [{'path': 'app.py', 'file_sha256': sha256(body.encode()), 'content': body}]}
        receipt = {'path': 'app.py', 'file_sha256': sha256(body.encode()), 'start': 2, 'end': 2, 'content': 'second', 'next_line': None}
        reduced = source_observation({'read_results': [receipt]}, material)['read_results'][0]
        self.assertNotIn('content', reduced)
        self.assertEqual(reduced['file_sha256'], receipt['file_sha256'])
        self.assertIn('current_code.files', reduced['content_location'])
        self.assertEqual(receipt['content'], 'second')
        self.assertEqual(source_observation(receipt, {'files': []}), receipt)
        self.assertEqual(source_observation(receipt | {'file_sha256': 'old-version'}, material)['content'], 'second')
        self.assertEqual(source_observation(receipt | {'content': 'different'}, material)['content'], 'different')

    def test_original_tests_and_independent_acceptance_cannot_be_edited(self):
        parent = self.analysis(original_tests=True)
        row = self.repair(parent)
        output = self.store.home / 'workspace'; output.mkdir()
        workspace = RepairWorkspace(self.store.home, row, output)
        for path in ['test_existing.py', '_acceptance/acceptance_division.py', '../app.py', '.env', 'tests/test_new.py', 'test/original.js', '__tests__/old.js']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                workspace.edit('add_source', {'path': path, 'content': 'pass'})
        with self.assertRaises(ValueError): workspace.edit('edit_source', {'path': 'test_existing.py', 'old': 'True', 'new': 'False'})
        with self.assertRaises(ValueError): workspace.edit('edit_source', {'path': 'app.py', 'old': 'missing', 'new': 'x'})

    def test_missing_runtime_rejects_before_creating_or_billing_and_duplicate_returns_original(self):
        parent = self.analysis()
        body = {'analysis_id': parent['id'], 'goal': '修好零分母处理。', 'request_key': uuid.uuid4().hex,
                'execution_ack': True, 'external_processing_ack': True}
        before = len(self.store.list())
        with patch('source_checks.require_runtime', side_effect=ValueError('runtime unavailable')):
            with self.assertRaises(ValueError): create_source_repair(self.store, body)
        self.assertEqual(len(self.store.list()), before)
        with patch('source_checks.require_runtime', return_value=copy.deepcopy(RUNTIME)):
            row = create_source_repair(self.store, body)
        with patch('source_checks.require_runtime', side_effect=AssertionError('must not probe duplicate')):
            self.assertEqual(create_source_repair(self.store, body)['id'], row['id'])
        with self.assertRaises(ValueError): create_source_repair(self.store, body | {'goal': 'another'})

    def test_pytest_original_tests_are_preserved_in_selected_repair_scope(self):
        self.source = import_source(self.store.home, 'code.zip', base.zipped([
            ('app.py', self.raw), ('test_pytest.py', b'def test_behavior():\n    assert False\n')]))
        parent = self.analysis()
        before = len(self.store.list())
        row = self.repair(parent)
        self.assertIn('test_pytest.py', row['paths'])
        self.assertEqual(len(self.store.list()), before + 1)

    def test_cancel_retains_sealed_candidate_and_never_enters_next_round(self):
        row, calls, inputs, _ = self.execute(self.repair(), cancel_after=2)
        self.assertEqual(row['status'], 'cancelled')
        self.assertEqual(len(self.report(row)['snapshots']), 1)
        self.assertIn('candidate-01.zip', row['artifacts'])
        self.assertEqual(len(inputs), 2)

    def test_source_tamper_blocks_all_artifact_preview(self):
        row = self.repair()
        changed = [False]
        def tamper(kwargs):
            if changed[0]: return None
            changed[0] = True
            path = self.store.home / 'sources' / self.source['id'] / 'files/app.py'
            path.write_bytes(self.raw + b'# changed')
            return base.tool_response('submit_changes', {'summary': 'invalid', 'remaining': []})
        ended, calls, _, _ = self.execute(row, callback=tamper)
        self.assertEqual(ended['agent_exit'], 'SourceIntegrityFailed')
        self.assertEqual(ended['artifacts'], {})

    def test_auth_or_platform_failure_never_opens_another_round(self):
        def fail(kwargs):
            error = RuntimeError('fixture authentication failure'); error.status_code = 401; raise error
        ended, calls, inputs, _ = self.execute(self.repair(), callback=fail)
        self.assertEqual(ended['status'], 'failed')
        self.assertEqual(len(calls), 2)
        self.assertEqual(inputs, [])

    def test_goal_planning_failure_does_not_start_author(self):
        def invalid(kwargs): return base.tool_response('submit_acceptance', {'criteria': ['goal'], 'tests': [{'path': 'acceptance_invalid.py', 'content': 'pass'}]})
        ended, calls, inputs, _ = self.execute(self.repair(), callback=invalid)
        self.assertEqual(ended['status'], 'failed')
        self.assertNotIn('repairer', ended['analysis_roles'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(inputs, [])

    def test_analysis_and_verifier_share_on_demand_project_reference_without_second_lookup(self):
        source = import_source(self.store.home, 'code.zip', base.zipped([('app.py', self.raw), ('README.md', b'divide: a zero divisor is invalid; the caller handles the exception.\n')]))
        row = create_analysis(self.store, self.body(source_id=source['id'], source_sha256=source['manifest_sha256'],
            paths=sorted(source['files']), analysis_mode='analysis_review', reference_mode='project'))
        calls, counts, issued = [], {'analyst': 0, 'reviewer': 0, 'reference': 0}, []
        question = {'question': '零分母行为有什么资料依据？', 'topics': ['divide'], 'version': '', 'urls': []}
        path = self.store.home / 'synthetic_shared.json'
        path.write_bytes(canonical_bytes({'historical_estimated_cost_cny': '0', 'historical_unknown_charge_reservation_cny': '0', 'blocked': False, 'requests': {}}))
        budget = SharedBudget(load_budget_config(), ledger_path=path, temp_dir=self.store.home / 'locks')
        def complete(**kwargs):
            calls.append(kwargs)
            names = {tool['function']['name'] for tool in kwargs['tools']}
            if 'submit_reference_advice' in names:
                counts['reference'] += 1
                data = json.loads(kwargs['messages'][1]['content']); ref = data['sources'][0]['source_ref']
                return base.tool_response('submit_reference_advice', {'status': 'answered', 'answer': '项目说明约定由调用方处理异常。', 'source_refs': ['rf-invalid' if counts['reference'] <= 3 else ref], 'unknowns': ['调用方实现未核实。']})
            if 'submit_case_review' in names:
                counts['reviewer'] += 1
                if counts['reviewer'] == 1: return base.tool_response('consult_references', question)
                data = json.loads(kwargs['messages'][1]['content'])
                self.assertTrue(data['reference_advice']['cached'])
                return base.tool_response('submit_case_review', {'verdict': 'needs_verification', 'category': 'uncertain',
                    'reason': '文档称由调用者处理，仍需核对实际调用方。', 'counterargument': '异常可能是正常合同。',
                    'conditions': '调用方是否捕获异常。', 'recommendation': '先核实调用方。', 'source_refs': ['R1']})
            counts['analyst'] += 1
            if counts['analyst'] == 1: return base.tool_response('read_source', {'path': 'app.py', 'start': 1, 'end': 2})
            if counts['analyst'] == 2:
                issued.append(json.loads(kwargs['messages'][-1]['content'])['evidence_id'])
                return base.tool_response('consult_references', question)
            return base.tool_response('submit_analysis', base.report(issued[0]))
        with patch('dotenv.dotenv_values', return_value=SETTINGS), patch('task_service_worker.RunBudget', return_value=budget), patch('review_model.litellm.completion', side_effect=complete):
            run_analysis(self.store, row['id'])
        ended = self.store.get(row['id'])
        self.assertEqual(ended['status'], 'finished', ended['reason'])
        self.assertEqual(counts, {'analyst': 3, 'reviewer': 2, 'reference': 4})
        report = self.report(ended)
        self.assertEqual(report['findings'][0]['review']['verdict'], 'needs_verification')
        record = report['reference_consultations'][0]
        self.assertEqual(record['used_by'], ['analyst', 'reviewer:F001'])
        self.assertEqual(record['sources'][0]['path'], 'README.md')
        self.assertEqual(len(json.loads(budget.path.read_bytes())['requests']), 9)

    def test_web_lookup_requires_explicit_consent_and_legacy_identity_is_unchanged(self):
        body = self.body()
        first = create_analysis(self.store, body)
        self.assertEqual(create_analysis(self.store, body | {'reference_mode': 'off'})['id'], first['id'])
        with self.assertRaises(ValueError): create_analysis(self.store, self.body(reference_mode='web'))
        row = create_analysis(self.store, self.body(reference_mode='web', reference_ack=True))
        self.assertEqual(row['reference_mode'], 'web')

    def test_repair_and_verifier_can_request_references_without_a_fourth_role(self):
        self.source = import_source(self.store.home, 'code.zip', base.zipped([
            ('app.py', self.raw), ('README.md', b'divide contract: raise ValueError for a zero divisor.\n')]))
        parent = self.analysis(references='project')
        asked, reference_calls = set(), []
        question = {'question': '零分母按什么约定处理？', 'topics': ['divide'], 'version': '', 'urls': []}
        def consult(kwargs):
            names = {tool['function']['name'] for tool in kwargs['tools']}
            data = json.loads(kwargs['messages'][1]['content'])
            if 'submit_reference_advice' in names:
                reference_calls.append(data)
                return base.tool_response('submit_reference_advice', {'status': 'answered', 'answer': '项目说明要求零分母抛ValueError。',
                    'source_refs': [data['sources'][0]['source_ref']], 'unknowns': ['仍需运行检查核实实现。']})
            key = (data['round'], 'author' if 'submit_changes' in names else 'reviewer')
            if 'consult_references' in names and key not in asked:
                asked.add(key)
                return base.tool_response('consult_references', question)
            return None
        row, calls, inputs, _ = self.execute(self.repair(parent), callback=consult)
        self.assertEqual(row['status'], 'finished', row['reason'])
        self.assertEqual(set(row['analysis_roles']), {'repairer', 'reviewer', 'reference'})
        self.assertEqual(len(reference_calls), 3)  # One per distinct source version; same-version facts reused.
        records = self.report(row)['reference_consultations']
        self.assertTrue(any('repairer:round-1' in record['used_by'] for record in records))
        self.assertTrue(any('reviewer:round-1' in record['used_by'] for record in records))
        retained = [json.loads(call['messages'][1]['content']) for call in calls if 'submit_changes' in {tool['function']['name'] for tool in call['tools']}]
        self.assertTrue(any(item['latest_observation'] and 'tree_sha256' in item['latest_observation'] and
                            item['reference_advice'] and item['reference_advice'][0]['sources'] for item in retained))

    def test_total_deadline_is_shared_across_rounds_and_sessions(self):
        row = self.repair()
        policy = dict(row['repair_policy'], parent_seconds=300)
        self.store.update(row['id'], repair_policy=policy)
        clock = [1000.0]
        def advance(kwargs): clock[0] += 50
        with patch('time.monotonic', side_effect=lambda: clock[0]):
            ended, calls, inputs, _ = self.execute(self.store.get(row['id']), callback=advance)
        self.assertEqual(ended['status'], 'failed')
        self.assertEqual(ended['agent_exit'], 'TimeoutError')
        self.assertLessEqual(len(self.report(ended)['rounds']), 2)
        self.assertLessEqual(len(calls), 6)
        self.assertEqual(ended['metrics']['total_elapsed_seconds'], 300)

    def test_source_repair_artifact_tamper_is_rejected_by_manifest(self):
        row, _, _, _ = self.execute(self.repair())
        path = Path(row['artifacts']['candidate-01.zip'])
        path.write_bytes(path.read_bytes() + b'tampered')
        with self.assertRaises(ValueError): registered_artifact(self.store, row, 'candidate-01.zip')

    def test_fake_check_runner_cannot_be_injected_into_production_storage(self):
        from types import SimpleNamespace
        with self.assertRaises(ValueError):
            execute_source_repair(SimpleNamespace(home=ROOT / 'data/not-created'), 'unused', check_runner=lambda: None)
        with self.assertRaises(ValueError):
            execute_source_repair(SimpleNamespace(home=ROOT / 'data/not-created'), 'unused', model_factory=lambda: None)

    def test_real_child_exit_after_candidate_commit_retains_code_without_replaying(self):
        from task_service_store import RunStore
        location = self.store.home / 'source-repair-child.json'
        result = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '--crash-source-repair', str(location)],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 73, result.stderr)
        saved = json.loads(location.read_bytes())
        reopened = RunStore(Path(saved['home'])); before = reopened.get(saved['run_id'])
        self.assertEqual(before['status'], 'running')
        archive = registered_artifact(reopened, before, 'candidate-01.zip')
        ledger = (reopened.home / 'synthetic_shared.json').read_bytes()
        self.assertEqual(len(json.loads(ledger)['requests']), 3)
        reopened.reconcile()
        after = reopened.get(saved['run_id'])
        self.assertEqual(after['status'], 'interrupted')
        self.assertEqual(registered_artifact(reopened, after, 'candidate-01.zip'), archive)
        self.assertEqual((reopened.home / 'synthetic_shared.json').read_bytes(), ledger)
        self.assertFalse(any(row['status'] == 'queued' for row in reopened.list()))


class ReferenceSafetyTests(unittest.TestCase):
    def setUp(self):
        proxy = patch('reference_sources.getproxies', return_value={})
        proxy.start(); self.addCleanup(proxy.stop)
    def test_only_registered_https_primary_sources_without_credentials_or_queries(self):
        for url in ['http://docs.python.org/a', 'https://127.0.0.1/a', 'https://docs.python.org.evil/a',
                    'https://user:pass@docs.python.org/a', 'https://docs.python.org:8443/a', 'https://docs.python.org/a?q=private',
                    'file:///etc/passwd', 'https://localhost/a']:
            with self.subTest(url=url), self.assertRaises(ValueError): public_url(url)
        self.assertEqual(public_url('https://docs.python.org/3.10/library/json.html#loads'), 'https://docs.python.org/3.10/library/json.html')

    def test_private_dns_and_redirects_cannot_be_fetched(self):
        with patch('reference_sources.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]), \
             patch('reference_sources.http.client.HTTPSConnection') as connection:
            with self.assertRaises(ValueError): read_public_page('https://docs.python.org/3.10/', deadline=time.monotonic() + 5)
            connection.assert_not_called()

    def test_excerpt_is_exact_captured_text_not_generated_summary(self):
        text = 'Header\n' + 'a' * 500 + '\nImportantContract: raise ValueError\n' + 'b' * 1000
        value = excerpt(text, ['ImportantContract'])
        self.assertIn(value, text)
        self.assertIn('ImportantContract', value)
        self.assertLessEqual(len(value.encode()), 1800)

    def test_public_dns_is_pinned_and_internal_redirect_and_pdf_are_rejected(self):
        class Response:
            status = 200
            def getheader(self, name, default=None): return 'text/plain' if name == 'Content-Type' else default
            def read(self, limit): return b'Captured primary-source text.'
        class Connection:
            response = Response()
            def __init__(self, *args, **kwargs): pass
            def request(self, *args, **kwargs): self._create_connection(('attacker.invalid', 443), 2, None)
            def getresponse(self): return self.response
            def close(self): pass
        with patch('reference_sources.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443))]), \
             patch('reference_sources.http.client.HTTPSConnection', Connection), patch('reference_sources.socket.create_connection') as connect:
            value = read_public_page('https://docs.python.org/3.10/', deadline=time.monotonic() + 5)
            self.assertEqual(value['text'], 'Captured primary-source text.')
            self.assertEqual(connect.call_args.args[0], ('8.8.8.8', 443))
            Connection.response = Mock(status=302)
            Connection.response.getheader.return_value = 'https://127.0.0.1/private'
            with self.assertRaises(ValueError): read_public_page('https://docs.python.org/3.10/', deadline=time.monotonic() + 5)
            Connection.response = Response()
            Connection.response.getheader = lambda name, default=None: 'application/pdf' if name == 'Content-Type' else default
            with self.assertRaises(ValueError): read_public_page('https://arxiv.org/pdf/1234.1234', deadline=time.monotonic() + 5)

    def test_trusted_system_proxy_keeps_original_https_host_verification(self):
        seen = {}
        class Connection:
            def __init__(self, host, port, **kwargs):
                seen.update(proxy_host=host, proxy_port=port, context=kwargs['context'])
            def set_tunnel(self, host, port): seen.update(origin_host=host, origin_port=port)
            def request(self, *args, **kwargs): pass
            def getresponse(self):
                return Mock(status=200, getheader=lambda name, default=None: 'text/plain' if name == 'Content-Type' else default,
                            read=lambda limit: b'Primary-source body through user proxy.')
            def close(self): pass
        with patch('reference_sources.getproxies', return_value={'https': 'http://127.0.0.1:7890'}), \
             patch('reference_sources.http.client.HTTPSConnection', Connection), patch('reference_sources.socket.getaddrinfo', side_effect=AssertionError('proxy resolves origin')):
            value = read_public_page('https://docs.python.org/3.10/', deadline=time.monotonic() + 5)
        self.assertEqual(seen['origin_host'], 'docs.python.org')
        self.assertTrue(seen['context'].check_hostname)
        self.assertEqual(value['connection_mode'], 'system_proxy_tls_origin_verified')


class SandboxTests(unittest.TestCase):
    def checks(self, output_text, exit_code=0):
        from source_checks import run_checks
        home = ROOT / 'temp/source_check_tests' / uuid.uuid4().hex
        home.mkdir(parents=True)
        commands = []
        class Process:
            def __init__(self, command, *, stdin, stdout, stderr):
                commands.append(command)
                self.returncode = exit_code
                value = json.load(stdin)
                assert value['files'] and value['acceptance']
                stdout.write(output_text.replace('SOURCE_CHECK_RESULT=', 'SOURCE_CHECK_RESULT_' + value['receipt'] + '=').encode()); stdout.flush()
            def poll(self): return self.returncode
        with patch('source_checks.inspect_image', return_value=RUNTIME['python']), patch('source_checks.subprocess.Popen', Process), \
             patch('source_pytest_bundle.pytest_bundle', return_value=({}, RUNTIME['python']['test_tools'])):
            result = run_checks({'app.py': b'raise RuntimeError("must never run on host")'},
                {'acceptance_goal.py': ACCEPTANCE}, RUNTIME, output=home, deadline=time.monotonic() + 10, check=lambda: None)
        return result, commands

    def test_checks_have_no_host_mount_network_credentials_or_model_commands(self):
        line = 'SOURCE_CHECK_RESULT=' + json.dumps({'tests': 2, 'errors': 0, 'failures': 0, 'skipped': 0}) + '\n'
        result, commands = self.checks(line)
        self.assertTrue(result['passed'])
        command = commands[0]
        for value in ['--interactive', '--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pull=never', '--pids-limit=64']:
            self.assertIn(value, command)
        for value in ['--mount', '-v', '--volume', '--privileged', '--env', '--network=host']:
            self.assertNotIn(value, command)
        self.assertIn(RUNTIME['python']['image_id'], command)

    def test_printed_success_exit_failure_zero_skipped_or_duplicate_reports_never_pass(self):
        good = 'SOURCE_CHECK_RESULT=' + json.dumps({'tests': 1, 'errors': 0, 'failures': 0, 'skipped': 0}) + '\n'
        empty = 'SOURCE_CHECK_RESULT=' + json.dumps({'tests': 0, 'errors': 0, 'failures': 0, 'skipped': 0}) + '\n'
        skipped = 'SOURCE_CHECK_RESULT=' + json.dumps({'tests': 1, 'errors': 0, 'failures': 0, 'skipped': 1}) + '\n'
        for text, code in [('passed=true', 0), (good, 1), (empty, 0), (skipped, 0), (good + good, 0)]:
            with self.subTest(text=text, code=code): self.assertFalse(self.checks(text, code)[0]['passed'])

    def test_missing_image_never_runs_uploaded_code_on_host(self):
        from source_checks import require_runtime
        with patch('source_checks.inspect_image', side_effect=ValueError('not available')), patch('source_checks.subprocess.Popen') as start:
            with self.assertRaises(ValueError): require_runtime(['app.py'])
            start.assert_not_called()

    def test_remote_docker_override_cannot_receive_user_source(self):
        from source_checks import inspect_image
        with patch.dict(os.environ, {'DOCKER_HOST': 'tcp://remote.example:2376'}), patch('source_checks.subprocess.run') as execute:
            with self.assertRaises(ValueError): inspect_image('python')
            execute.assert_not_called()

    def test_public_pytest_bundle_works_without_original_site_packages(self):
        from source_pytest_bundle import pytest_bundle
        files, binding = pytest_bundle()
        self.assertIn('pytest', binding['versions'])
        self.assertGreater(binding['files'], 10)
        self.assertFalse(any(name.endswith(('.pyd', '.so', '.env')) for name in files))
        root = ROOT / 'temp/pytest_bundle_checks' / uuid.uuid4().hex
        tools = root / 'tools'
        for name, raw in files.items():
            target = tools / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(raw)
        test = root / 'test_public_fixture.py'
        test.write_text('import unittest\ndef test_plain(): assert 2 + 2 == 4\nclass TestPublic(unittest.TestCase):\n    def test_case(self): self.assertEqual(6 / 2, 3)\n', encoding='utf8')
        config = root / 'pytest.ini'; config.write_text('[pytest]\n', encoding='utf8')
        code = ('import os,sys\n'
                'sys.path=[sys.argv[1]]+[p for p in sys.path if "site-packages" not in p]\n'
                'os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"]="1"\n'
                'def guard(event,args):\n'
                '    if event in {"socket.connect","socket.getaddrinfo"}: raise PermissionError("offline tool check")\n'
                'sys.addaudithook(guard)\n'
                'import pytest\n'
                'raise SystemExit(pytest.main(["-q","-p","no:cacheprovider","-c",sys.argv[2],sys.argv[3]]))\n')
        result = subprocess.run([sys.executable, '-B', '-I', '-c', code, str(tools), str(config), str(test)], cwd=root,
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('2 passed', result.stdout)


class SourceHttpTests(unittest.TestCase):
    def setUp(self):
        from task_service import make_handler
        self.fixture = WorkflowTests(); self.fixture.setUp()
        self.store = self.fixture.store
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
        self.server.RequestHandlerClass = make_handler(self.store, 'source-test-token', self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)

    def request(self, path, body=None):
        value = urllib.request.Request('http://127.0.0.1:' + str(self.server.server_port) + path,
            data=canonical_bytes(body) if body is not None else None, headers={'X-Task-Token': 'source-test-token'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(value, timeout=10) as response: return response.read()

    def test_goal_only_creation_two_round_result_zip_and_frozen_acceptance_download(self):
        parent = self.fixture.analysis()
        body = {'analysis_id': parent['id'], 'goal': '零分母明确报ValueError，保留正常除法。', 'request_key': uuid.uuid4().hex,
                'execution_ack': True, 'external_processing_ack': True}
        with patch('source_checks.require_runtime', return_value=copy.deepcopy(RUNTIME)):
            row = json.loads(self.request('/api/source-repair', body))
            self.assertEqual(json.loads(self.request('/api/source-repair', body))['id'], row['id'])
        ended, _, _, _ = self.fixture.execute(row)
        report = json.loads(self.request('/api/runs/' + row['id'] + '/artifacts/report.json?download=1'))
        self.assertEqual(report['repair_result'], 'offline_flow_completed')
        self.assertEqual(len(report['rounds']), 2)
        archive = self.request('/api/runs/' + row['id'] + '/artifacts/candidate-02.zip?download=1')
        with zipfile.ZipFile(io.BytesIO(archive)) as value: self.assertIn(b'raise ValueError', value.read('app.py'))
        self.assertIn('tests_sha256', json.loads(self.request('/api/runs/' + row['id'] + '/artifacts/acceptance.json?download=1')))
        page = self.request('/').decode()
        self.assertIn('无需逐条挑技术问题', page)
        self.assertIn('/source-workflow-panel.js', page)
        self.assertIn('renderSourceRepair', self.request('/source-workflow-panel.js').decode())
        # Browser <script src> sends no API token. Test the actual browser request,
        # rather than only the authenticated helper used for report downloads.
        url = 'http://127.0.0.1:' + str(self.server.server_port) + '/source-workflow-panel.js'
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=10) as response:
            self.assertIn('javascript', response.headers['Content-Type'])
            self.assertIn(b'renderSourceRepair', response.read())

    def test_environment_and_web_consent_are_enforced_without_custom_commands(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/source-repair-runtime/prepare', {'image': 'untrusted'})
        self.assertEqual(error.exception.code, 400)
        with patch('source_checks.runtime_status', return_value={'available': {}, 'missing': ['python'], 'notice': 'offline fixture', 'limits': 'fixture'}):
            self.assertEqual(json.loads(self.request('/api/source-repair-runtime/inspect', {}))['notice'], 'offline fixture')
        body = self.fixture.body(reference_mode='web')
        with self.assertRaises(urllib.error.HTTPError) as error: self.request('/api/analysis', body)
        self.assertEqual(error.exception.code, 400)
        row = json.loads(self.request('/api/analysis', body | {'reference_ack': True}))
        self.assertEqual(row['reference_mode'], 'web')
        self.request('/api/runs/' + row['id'] + '/cancel', {})


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--crash-source-repair':
        fixture = WorkflowTests(); fixture.setUp(); row = fixture.repair()
        original = fixture.store.update
        def stop_after_seal(run_id, **kwargs):
            value = original(run_id, **kwargs)
            if kwargs.get('event') == 'repair_candidate_sealed':
                Path(sys.argv[2]).write_bytes(canonical_bytes({'home': str(fixture.store.home), 'run_id': run_id}))
                os._exit(73)
            return value
        with patch.object(fixture.store, 'update', side_effect=stop_after_seal): fixture.execute(row)
        raise SystemExit('Expected checkpoint exit did not happen')
    else: unittest.main(verbosity=2)
