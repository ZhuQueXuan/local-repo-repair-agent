"""Offline invariants for v2 materials, dispatch, partial results and shared guards.

Every completion is a local substitute. These tests do not measure model quality.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from analysis_runtime import AnalysisBroker, create_analysis, report_text
from analysis_review import reviewed_report
from review_cases import ReviewCases, MaterialIncomplete, RequestCaseEvidence, PACKET_LIMIT, CASE_SCHEMAS
from collaboration_protocol import canonical_bytes, sha256
from collaboration_panel import registered_artifact
from analysis_sources import import_source
import test_analysis as base


def decision(ref='R1', verdict='supported'):
    return {'verdict': verdict, 'category': 'uncertain' if verdict == 'needs_verification' else 'defect_candidate',
            'reason': '离线夹具，引用当前输入源码；不是模型质量成绩。',
            'counterargument': '调用者可能保证非零输入，异常不必然是产品缺陷。',
            'conditions': '需核实调用合同与异常处理路径。', 'recommendation': '先确认合同再决定修复。',
            'source_refs': [ref]}


def evidence(path='extra.py', start=1, end=83):
    return {'question': '需要核对调用方完整边界。', 'ranges': [{'path': path, 'start': start, 'end': end}]}


class CaseTests(unittest.TestCase):
    setUp = base.AnalysisTests.setUp
    body = base.AnalysisTests.body
    scripted_run = base.AnalysisTests.scripted_run

    def row(self, **overrides):
        return create_analysis(self.store, self.body(analysis_mode='analysis_review', **overrides))

    def controller(self, count=1, source=None, long=False):
        overrides = ({'source_id': source['id'], 'source_sha256': source['manifest_sha256'], 'paths': sorted(source['files'])} if source else {})
        row = self.row(**overrides)
        analyst = AnalysisBroker(self.store.home, row)
        ref = analyst.span('app.py', 1, 2)
        value = base.report(ref['evidence_id'])
        value['findings'] = [copy.deepcopy(value['findings'][0]) for _ in range(count)]
        if long:
            value['findings'][0]['reasoning'] = '完整的长问题正文，不得通过字符串分页截断。' * 45
        analyst.dispatch('submit_analysis', value)
        output = self.store.home / ('material-' + row['id'])
        output.mkdir()
        (output / 'draft-report.json').write_bytes(canonical_bytes(analyst.report))
        return ReviewCases(self.store.home, row, output, sha256(canonical_bytes(analyst.report)))

    def script(self, review=None, count=1, empty=False):
        ordinal = [0]
        def complete(kwargs):
            names = {tool['function']['name'] for tool in kwargs['tools']}
            if 'submit_case_review' in names:
                if review:
                    return review(kwargs)
                return base.tool_response('submit_case_review', decision())
            ordinal[0] += 1
            if ordinal[0] == 1:
                return base.tool_response('read_source', {'path': 'app.py', 'start': 1, 'end': 2})
            ref = json.loads(kwargs['messages'][-1]['content'])['evidence_id']
            value = base.report(ref)
            value['findings'] = [] if empty else [copy.deepcopy(value['findings'][0]) for _ in range(count)]
            return base.tool_response('submit_analysis', value)
        return complete

    def read_report(self, row):
        return json.loads(registered_artifact(self.store, row, 'report.json'))

    def test_new_rows_use_v2_and_idempotency_returns_saved_v1_without_reinterpretation(self):
        body = self.body(analysis_mode='analysis_review')
        row = create_analysis(self.store, body)
        self.assertEqual(row['profile'], 'readonly_analysis_review_v2')
        self.store.update(row['id'], profile='readonly_analysis_review_v1')
        self.assertEqual(create_analysis(self.store, body)['profile'], 'readonly_analysis_review_v1')
        self.assertEqual(row['analysis_policy'], {'analyst_seconds': 300, 'reviewer_seconds': 180, 'parent_seconds': 480})

    def test_full_long_finding_and_83_line_source_stay_in_same_packet(self):
        raw = self.raw + b'# source\n' * 81
        source = import_source(self.store.home, 'app.py', raw)
        owner = self.controller(source=source, long=True)
        case = owner.cases[0]
        case.prepare()
        packet = json.loads(case.messages()[1]['content'])
        self.assertEqual(packet['candidate']['reasoning'], owner.draft['findings'][0]['reasoning'])
        self.assertEqual(packet['candidate']['trigger'], owner.draft['findings'][0]['trigger'])
        self.assertEqual(packet['source_material'][0]['end'], 83)
        self.assertEqual(packet['source_material'][0]['content'], '\n'.join(raw.decode().splitlines()))
        self.assertLessEqual(len(canonical_bytes(case.messages())), PACKET_LIMIT)
        self.assertNotIn('evidence_ids', packet['candidate'])

    def test_large_source_falls_back_to_complete_citations_and_explicit_missing_inventory(self):
        source = import_source(self.store.home, 'code.zip', base.zipped([
            ('app.py', self.raw), ('extra.py', b'# ' + b'x' * (PACKET_LIMIT * 2))]))
        owner = self.controller(source=source)
        case = owner.cases[0]; case.prepare()
        packet = case.payload()
        self.assertIn('extra.py', packet['scope']['files_not_provided'])
        self.assertEqual(packet['candidate']['trigger'], owner.draft['findings'][0]['trigger'])
        self.assertEqual(packet['source_material'][0]['content'], '\n'.join(self.raw.decode().splitlines()))

    def test_oversize_core_is_not_silently_truncated_or_sent(self):
        owner = self.controller(long=True)
        with patch('review_cases.PACKET_LIMIT', 400), self.assertRaises(MaterialIncomplete):
            owner.cases[0].prepare()
        self.assertNotIn('assessment', owner.cases[0].result)

    def test_supplement_83_lines_preserves_core_and_existing_R_bindings(self):
        source = import_source(self.store.home, 'code.zip', base.zipped([
            ('app.py', self.raw), ('extra.py', b'# e\n' * 83), ('large.py', b'# ' + b'x' * (PACKET_LIMIT * 2))]))
        owner = self.controller(source=source, long=True)
        case = owner.cases[0]; case.prepare()
        original = copy.deepcopy(case.payload())
        case.add_evidence([RequestCaseEvidence.parse(evidence())])
        current = case.payload()
        self.assertEqual(current['candidate'], original['candidate'])
        self.assertEqual(current['source_material'][:len(original['source_material'])], original['source_material'])
        self.assertEqual(current['source_material'][-1]['end'], 83)
        self.assertLessEqual(len(canonical_bytes(case.messages())), PACKET_LIMIT)
        with self.assertRaises(MaterialIncomplete): case.add_evidence([RequestCaseEvidence.parse(evidence())])

    def test_repeated_and_oversize_evidence_do_not_remove_core_or_create_verdict(self):
        owner = self.controller(); case = owner.cases[0]; case.prepare()
        before = copy.deepcopy(case.refs)
        with self.assertRaises(MaterialIncomplete):
            case.add_evidence([RequestCaseEvidence.parse(evidence('app.py', 1, 2))])
        self.assertEqual(case.refs, before)
        self.assertNotIn('assessment', case.result)
        source = import_source(self.store.home, 'code.zip', base.zipped([('app.py', self.raw), ('extra.py', b'# ' + b'x' * (PACKET_LIMIT * 2))]))
        owner = self.controller(source=source); case = owner.cases[0]; case.prepare()
        before = copy.deepcopy(case.refs)
        with self.assertRaises(MaterialIncomplete): case.add_evidence([RequestCaseEvidence.parse(evidence('extra.py', 1, 1))])
        self.assertEqual(case.refs, before)

    def test_fresh_case_evidence_cannot_reuse_analyst_or_other_case_identity(self):
        owner = self.controller(count=2)
        for case in owner.cases: case.prepare()
        old = next(iter(owner.draft['evidence']))
        with self.assertRaises(ValueError): owner.cases[0].submit(decision(old))
        with self.assertRaises(ValueError): owner.cases[0].submit(decision('app.py:1'))
        other = owner.cases[1].evidence()['R1']['evidence_id']
        with self.assertRaises(ValueError): owner.cases[0].submit(decision(other))
        self.assertNotEqual(owner.cases[0].evidence()['R1']['evidence_id'], other)
        for case in owner.cases: case.submit(decision())
        final = reviewed_report(owner.draft, owner.report())
        self.assertEqual(len(final['review']['assessments']), 2)

    def test_source_and_draft_tamper_still_block_submission(self):
        owner = self.controller(); case = owner.cases[0]; case.prepare()
        path = self.store.home / 'sources' / self.source['id'] / 'files/app.py'
        path.write_bytes(self.raw + b'# changed')
        with self.assertRaises(ValueError): case.submit(decision())
        self.assertNotIn('assessment', case.result)
        owner.reader.output.joinpath('draft-report.json').write_bytes(b'{}')
        with self.assertRaises(ValueError): owner.verify()

    def test_three_fresh_contexts_program_aggregation_and_case_attribution(self):
        verdicts = iter(('supported', 'rejected', 'needs_verification'))
        def review(kwargs):
            self.assertEqual(len(kwargs['messages']), 2)
            self.assertFalse(any(message['role'] in {'assistant', 'tool'} for message in kwargs['messages']))
            self.assertFalse(kwargs['parallel_tool_calls'])
            self.assertEqual({item['function']['name'] for item in kwargs['tools']}, set(CASE_SCHEMAS))
            self.assertNotEqual(kwargs['tools'][0]['function']['description'], kwargs['tools'][1]['function']['description'])
            packet = json.loads(kwargs['messages'][1]['content'])
            submit = next(tool['function'] for tool in kwargs['tools'] if tool['function']['name'] == 'submit_case_review')
            self.assertEqual(submit['parameters']['properties']['source_refs']['items']['enum'],
                             [item['ref'] for item in packet['source_material']])
            return base.tool_response('submit_case_review', decision(verdict=next(verdicts)))
        row, calls, budget = self.scripted_run(self.row(), self.script(review, count=3))
        self.assertEqual(row['status'], 'finished')
        self.assertEqual(row['review_status'], 'completed')
        self.assertEqual(len(calls), 5)  # Analyst2 + one decision per case; no LLM finalizer.
        self.assertTrue(all(request['parallel_tool_calls'] is False for request in calls))
        self.assertEqual(len(json.loads(budget.path.read_bytes())['requests']), 5)
        roles = row['analysis_roles']['reviewer']['cases']
        self.assertEqual(len({item['session_id'] for item in roles}), 3)
        events = [event['data'] for event in self.store.events(row['id']) if event['kind'] == 'analysis_role_usage_reserved']
        self.assertEqual([item.get('case_id') for item in events], [None, None, 'F001', 'F002', 'F003'])
        result = self.read_report(row)
        self.assertEqual([item['review']['verdict'] for item in result['findings']], ['supported', 'rejected', 'needs_verification'])
        log = (self.store.home / 'runs' / row['id'] / 'reviewer-api-requests.jsonl').read_text(encoding='utf8')
        entries = [json.loads(line) for line in log.splitlines()]
        self.assertEqual(len(entries), 3)
        self.assertTrue(all(entry['tools_sha256'] and entry['case_id'] for entry in entries))
        for entry, request in zip(entries, calls[2:]):
            self.assertEqual(entry['tools_sha256'], sha256(canonical_bytes(request['tools'])))
            self.assertEqual(entry['messages'], request['messages'])
            for key, value in entry['effective_parameters'].items():
                self.assertEqual(value, request.get(key))
        self.assertNotIn('primary-test-secret', log)
        self.assertNotIn('backup-test-secret', log)
        self.assertIn('本条执行状态', report_text(result))

    def test_invalid_first_case_is_bounded_and_does_not_block_next(self):
        def review(kwargs):
            packet = json.loads(kwargs['messages'][1]['content'])
            return base.tool_response('submit_case_review', decision('R999' if packet['case_id'] == 'F001' else 'R1'))
        row, calls, _ = self.scripted_run(self.row(), self.script(review, count=2))
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['review_status'], 'partial')
        self.assertEqual(len(calls), 11)  # Analyst2 + 8 stagnant errors + next case1.
        result = self.read_report(row)
        self.assertNotIn('review', result['findings'][0])
        self.assertEqual(result['findings'][0]['review_execution']['execution_status'], 'model_not_submitted')
        self.assertEqual(result['findings'][1]['review']['verdict'], 'supported')
        self.assertEqual(len(result['review']['assessments']), 1)
        self.assertIn('未完成项没有模型结论', result['note'])

    def test_format_recovery_can_continue_past_three_total_case_requests(self):
        attempts = 0
        def review(kwargs):
            nonlocal attempts
            attempts += 1
            return base.tool_response('submit_case_review', decision('R999' if attempts <= 3 else 'R1'))
        ended, calls, _ = self.scripted_run(self.row(), self.script(review))
        self.assertEqual(ended['status'], 'finished', ended['reason'])
        self.assertEqual(attempts, 4)
        self.assertEqual(len(calls), 6)

    def test_repeated_read_is_stopped_locally_without_eight_more_queries(self):
        def review(kwargs): return base.tool_response('request_case_evidence', evidence('app.py', 1, 2))
        row, calls, _ = self.scripted_run(self.row(), self.script(review, count=2))
        self.assertEqual(len(calls), 4)
        self.assertEqual(row['review_status'], 'failed')
        result = self.read_report(row)
        self.assertEqual(result['review']['assessments'], [])
        self.assertTrue(all(item['execution_status'] == 'material_incomplete' for item in result['review']['case_results']))

    def test_no_findings_uses_overview_case_without_invented_findings(self):
        row, calls, _ = self.scripted_run(self.row(), self.script(empty=True))
        result = self.read_report(row)
        self.assertEqual(len(calls), 3)
        self.assertEqual(result['findings'], [])
        self.assertEqual(result['review']['assessments'], [])
        self.assertEqual(result['review']['overview_review']['verdict'], 'supported')

    def test_cancel_second_case_keeps_first_real_judgment_and_usage(self):
        row = self.row()
        def review(kwargs):
            if json.loads(kwargs['messages'][1]['content'])['case_id'] == 'F002': self.store.cancel(row['id'])
            return base.tool_response('submit_case_review', decision())
        ended, calls, _ = self.scripted_run(row, self.script(review, count=3))
        self.assertEqual(ended['status'], 'cancelled')
        self.assertEqual(len(calls), 4)
        result = self.read_report(ended)
        self.assertEqual(len(result['review']['assessments']), 1)
        self.assertEqual([item['execution_status'] for item in result['review']['case_results']], ['submitted', 'cancelled', 'cancelled'])

    def test_deadline_is_not_reset_between_cases(self):
        clock = [1000.0]
        def review(kwargs):
            clock[0] += 100
            return base.tool_response('submit_case_review', decision())
        with patch('time.monotonic', side_effect=lambda: clock[0]):
            row, calls, _ = self.scripted_run(self.row(), self.script(review, count=3))
        self.assertEqual(len(calls), 4)
        result = self.read_report(row)
        self.assertEqual(len(result['review']['assessments']), 1)
        self.assertEqual(result['review']['case_results'][1]['execution_status'], 'timeout')
        self.assertEqual(result['review']['case_results'][2]['execution_status'], 'timeout')

    def test_both_platform_auth_failure_stops_all_cases_without_new_session_retry(self):
        def review(kwargs):
            error = RuntimeError('fixture authentication failure')
            error.status_code = 401
            raise error
        row, calls, _ = self.scripted_run(self.row(), self.script(review, count=3))
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(len(calls), 4)  # Original gateway -> official fallback, then global stop.
        result = self.read_report(row)
        self.assertEqual(result['review']['assessments'], [])
        self.assertTrue(all(item['execution_status'] == 'request_failed' for item in result['review']['case_results']))

    def test_forged_success_state_without_model_assessment_rejected(self):
        owner = self.controller()
        value = owner.report()
        value['case_results'][0]['execution_status'] = 'submitted'
        with self.assertRaises(ValueError): reviewed_report(owner.draft, value)

    def test_two_evidence_calls_are_one_round_and_preserve_full_core_in_actual_wire(self):
        source = import_source(self.store.home, 'code.zip', base.zipped([
            ('app.py', self.raw), ('extra.py', b'# extra\n' * 83), ('large.py', b'# ' + b'x' * (PACKET_LIMIT * 2))]))
        row = self.row(source_id=source['id'], source_sha256=source['manifest_sha256'], paths=sorted(source['files']))
        seen = []
        def review(kwargs):
            payload = json.loads(kwargs['messages'][1]['content'])
            seen.append(payload)
            submit = next(tool['function'] for tool in kwargs['tools'] if tool['function']['name'] == 'submit_case_review')
            self.assertEqual(submit['parameters']['properties']['source_refs']['items']['enum'],
                             [item['ref'] for item in payload['source_material']])
            if len(seen) == 1:
                one = base.tool_response('request_case_evidence', evidence('extra.py', 1, 40))
                two = base.tool_response('request_case_evidence', evidence('extra.py', 41, 83))
                one.choices[0].message.tool_calls.append(two.choices[0].message.tool_calls[0])
                return one
            self.assertEqual(payload['candidate'], seen[0]['candidate'])
            self.assertEqual(payload['source_material'][0], seen[0]['source_material'][0])
            self.assertEqual(payload['source_material'][-1]['end'], 83)
            self.assertEqual(payload['evidence_request']['status'], 'provided')
            return base.tool_response('submit_case_review', decision('R3', 'needs_verification'))
        ended, calls, _ = self.scripted_run(row, self.script(review))
        self.assertEqual(ended['status'], 'finished')
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(seen), 2)
        self.assertEqual(self.read_report(ended)['findings'][0]['review']['verdict'], 'needs_verification')

    def test_source_tamper_after_model_reply_blocks_all_artifact_publication(self):
        def review(kwargs):
            file = self.store.home / 'sources' / self.source['id'] / 'files/app.py'
            file.write_bytes(self.raw + b'# changed')
            return base.tool_response('submit_case_review', decision())
        ended, calls, _ = self.scripted_run(self.row(), self.script(review))
        self.assertEqual(len(calls), 3)
        self.assertEqual(ended['agent_exit'], 'SourceIntegrityFailed')
        self.assertEqual(ended['artifacts'], {})
        self.assertEqual(ended['review_status'], 'failed')
        self.assertNotEqual(ended['analysis_roles']['reviewer']['status'], 'running')

    def test_failover_route_is_shared_across_case_sessions(self):
        attempts = []
        def review(kwargs):
            attempts.append(kwargs['model'])
            if len(attempts) == 1:
                error = RuntimeError('fixture gateway unavailable')
                error.status_code = 503
                raise error
            return base.tool_response('submit_case_review', decision())
        ended, calls, budget = self.scripted_run(self.row(), self.script(review, count=2))
        self.assertEqual(ended['status'], 'finished')
        self.assertEqual(attempts, ['openai/deepseek-v4-flash', 'openai/deepseek-flash', 'openai/deepseek-flash'])
        self.assertEqual(ended['metrics']['requests'], 5)
        self.assertEqual(len(json.loads(budget.path.read_bytes())['requests']), 5)
        entries = [json.loads(line) for line in (self.store.home / 'runs' / ended['id'] / 'reviewer-api-requests.jsonl').read_text(encoding='utf8').splitlines()]
        self.assertEqual([entry['case_id'] for entry in entries], ['F001', 'F001', 'F002'])

    def test_submitted_case_is_immutable_and_never_reopens_evidence(self):
        owner = self.controller(); case = owner.cases[0]; case.prepare()
        first = case.submit(decision())
        self.assertEqual(case.submit(decision()), first)
        with self.assertRaises(ValueError): case.submit(decision(verdict='rejected'))
        with self.assertRaises(ValueError): case.add_evidence([RequestCaseEvidence.parse(evidence('app.py', 1, 2))])

    def test_registered_case_checkpoint_survives_disk_reopen_and_restart_without_new_chance(self):
        from task_service_store import RunStore
        # An actual child exits after the checkpoint DB commit. Never bypass the
        # terminal-row guard to manufacture a running record from a finished one.
        location = self.store.home / 'checkpoint-child-location.json'
        process = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '--checkpoint-exit-fixture', str(location)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=60)
        self.assertEqual(process.returncode, 73, process.stderr)
        saved = json.loads(location.read_bytes())
        reopened = RunStore(Path(saved['home']))
        row = reopened.get(saved['run_id'])
        self.assertEqual(row['status'], 'running')
        checkpoint = 'review-checkpoint-001.json'
        original = registered_artifact(reopened, row, checkpoint)
        value = json.loads(original)
        self.assertEqual(value['status'], 'partial')
        self.assertEqual([item['execution_status'] for item in value['case_results']], ['submitted', 'not_started'])
        ledger_path = reopened.home / 'synthetic_shared.json'
        ledger_before = ledger_path.read_bytes()
        self.assertEqual(len(json.loads(ledger_before)['requests']), 3)
        reopened.reconcile()
        ended = reopened.get(row['id'])
        self.assertEqual(ended['status'], 'interrupted')
        self.assertEqual(ended['review_status'], 'interrupted')
        self.assertEqual(ended['analysis_roles']['reviewer']['status'], 'interrupted')
        self.assertEqual(ended['analysis_roles']['reviewer']['cases'][0]['status'], 'submitted')
        self.assertEqual(registered_artifact(reopened, ended, checkpoint), original)
        self.assertEqual(ledger_path.read_bytes(), ledger_before)
        self.assertFalse(any(item['status'] == 'queued' for item in reopened.list()))


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--checkpoint-exit-fixture':
        fixture = CaseTests()
        fixture.setUp()
        row = fixture.row()
        update = fixture.store.update
        def exit_after_checkpoint(run_id, **kwargs):
            result = update(run_id, **kwargs)
            if kwargs.get('event') == 'analysis_case_checkpoint':
                Path(sys.argv[2]).write_bytes(canonical_bytes({'home': str(fixture.store.home), 'run_id': run_id}))
                os._exit(73)
            return result
        with patch.object(fixture.store, 'update', side_effect=exit_after_checkpoint):
            fixture.scripted_run(row, fixture.script(count=2))
        raise SystemExit('checkpoint fixture failed to exit')
    else:
        unittest.main(verbosity=2)
