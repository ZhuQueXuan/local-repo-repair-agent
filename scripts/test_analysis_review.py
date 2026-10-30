"""Fresh-session review, immutable handoff, per-finding coverage and failure retention.

Responses are deliberately scripted, not an evaluation of model judgment.
"""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from analysis_runtime import AnalysisBroker, create_analysis
from analysis_review import AnalysisReviewBroker, REVIEW_SCHEMAS, reviewed_report
from collaboration_panel import registered_artifact
from collaboration_protocol import canonical_bytes, sha256
import test_analysis as analysis_tests
from test_analysis import report, tool_response


def assessment(finding_id, evidence_id, verdict='supported'):
    return {'finding_id': finding_id, 'verdict': verdict,
        'category': {'supported': 'defect_candidate', 'rejected': 'improvement', 'needs_verification': 'uncertain'}[verdict],
        'reason': '脚本夹具：按实际代码路径核对，不能视为模型质量成绩。',
        'counterargument': '调用者可能已限制输入，缺少契约时不能直接断言产品缺陷。',
        'conditions': '只有允许零分母且没有上层异常处理时，才会影响该调用。',
        'recommendation': '先确认接口约定，再决定是否需要修复。', 'evidence_ids': [evidence_id]}


def review_payload(digest, items, evidence_id):
    return {'draft_sha256': digest, 'summary': '脚本复核完成，保留不同意见。', 'overview_assessment': '概览与所读代码对应，未通读全仓。',
        'assessments': items, 'unknowns': ['未运行测试，未读取调用方，结论仍需验证。'], 'evidence_ids': [evidence_id]}


class ReviewTests(unittest.TestCase):
    setUp = analysis_tests.AnalysisTests.setUp
    body = analysis_tests.AnalysisTests.body
    scripted_run = analysis_tests.AnalysisTests.scripted_run

    def row(self, **overrides):
        return create_analysis(self.store, self.body(analysis_mode='analysis_review', **overrides))

    def broker(self, count=1, long=False):
        row = self.row()
        analyst = AnalysisBroker(self.store.home, row)
        ref = analyst.span('app.py', 1, 2)
        value = report(ref['evidence_id'])
        value['findings'] = [copy.deepcopy(value['findings'][0]) for _ in range(count)]
        if long:
            value['findings'][0]['reasoning'] = '完整依据仍需核对。' * 100
        analyst.dispatch('submit_analysis', value)
        output = self.store.home / 'broker'
        output.mkdir(exist_ok=True)
        (output / 'draft-report.json').write_bytes(canonical_bytes(analyst.report))
        digest = sha256(canonical_bytes(analyst.report))
        return analyst.report, AnalysisReviewBroker(self.store.home, row, output, digest)

    def read_draft(self, broker, section, finding_id=None):
        args = {'section': section} | ({'finding_id': finding_id} if finding_id else {})
        while True:
            receipt = broker.dispatch('read_draft', args)
            if receipt['next_offset'] is None:
                return receipt
            args['offset'] = receipt['next_offset']

    def test_mode_identity_preserves_old_single_client_and_blocks_reusing_key_for_dual(self):
        body = self.body()
        legacy = create_analysis(self.store, body)
        self.assertEqual(legacy['analysis_mode'], 'single')
        self.assertEqual(create_analysis(self.store, body | {'analysis_mode': 'single'})['id'], legacy['id'])
        with self.assertRaises(ValueError): create_analysis(self.store, body | {'analysis_mode': 'analysis_review'})
        with self.assertRaises(ValueError): create_analysis(self.store, body | {'analysis_mode': 'unregistered'})
        dual = self.row()
        self.assertEqual(dual['analysis_policy'], {'analyst_seconds': 300, 'reviewer_seconds': 180, 'parent_seconds': 480})

    def test_draft_evidence_cannot_substitute_fresh_source_reads(self):
        draft, broker = self.broker()
        self.read_draft(broker, 'overview'); self.read_draft(broker, 'finding', 'F001')
        old = next(iter(draft['evidence']))
        with self.assertRaises(ValueError):
            broker.dispatch('submit_analysis_review', review_payload(broker.draft_sha256, [assessment('F001', old)], old))
        fresh = broker.dispatch('read_source', {'path': 'app.py', 'start': 1, 'end': 2})['evidence_id']
        self.assertTrue(fresh.startswith('rv-'))
        self.assertNotEqual(fresh, old)
        broker.dispatch('submit_analysis_review', review_payload(broker.draft_sha256, [assessment('F001', fresh)], fresh))
        self.assertEqual(broker.report['status'], 'completed')

    def test_missing_duplicate_new_finding_ids_or_wrong_version_rejected(self):
        _, broker = self.broker(count=2)
        self.read_draft(broker, 'overview')
        for identity in ('F001', 'F002'): self.read_draft(broker, 'finding', identity)
        fresh = broker.span('app.py', 1, 2)['evidence_id']
        for ids in [('F001',), ('F001', 'F001'), ('F001', 'invented')]:
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                broker.dispatch('submit_analysis_review', review_payload(broker.draft_sha256, [assessment(key, fresh) for key in ids], fresh))
        with self.assertRaises(ValueError):
            broker.dispatch('submit_analysis_review', review_payload('a' * 64, [assessment(key, fresh) for key in ('F001', 'F002')], fresh))

    def test_partial_draft_page_does_not_certify_full_read(self):
        _, broker = self.broker(long=True)
        self.read_draft(broker, 'overview')
        receipt = broker.dispatch('read_draft', {'section': 'finding', 'finding_id': 'F001', 'limit': 30})
        self.assertIsNotNone(receipt['next_offset'])
        fresh = broker.span('app.py', 1, 2)['evidence_id']
        value = review_payload(broker.draft_sha256, [assessment('F001', fresh)], fresh)
        with self.assertRaises(ValueError): broker.dispatch('submit_analysis_review', value)
        # Reading the tail without the missing middle must not count either.
        broker.dispatch('read_draft', {'section': 'finding', 'finding_id': 'F001', 'offset': receipt['total_characters'] - 10})
        with self.assertRaises(ValueError): broker.dispatch('submit_analysis_review', value)
        self.read_draft(broker, 'finding', 'F001')
        broker.dispatch('submit_analysis_review', value)

    def test_source_and_draft_tamper_and_execute_tools_are_rejected(self):
        _, broker = self.broker()
        for name in ('bash', 'submit_analysis', 'write_file'):
            with self.subTest(tool=name), self.assertRaises(ValueError): broker.dispatch(name, {})
        source = self.store.home / 'sources' / self.source['id'] / 'files/app.py'
        source.write_bytes(self.raw + b'# changed')
        with self.assertRaises(ValueError): broker.span('app.py', 1, 2)
        path = broker.output / 'draft-report.json'
        path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaises(ValueError): broker.dispatch('list_draft_findings', {})

    def test_three_verdicts_are_preserved_and_never_rewrite_original_findings(self):
        draft, broker = self.broker(count=3)
        original = canonical_bytes(draft)
        self.read_draft(broker, 'overview')
        for identity in ('F001', 'F002', 'F003'): self.read_draft(broker, 'finding', identity)
        fresh = broker.span('app.py', 1, 2)['evidence_id']
        items = [assessment(identity, fresh, verdict) for identity, verdict in zip(('F001', 'F002', 'F003'), ('supported', 'rejected', 'needs_verification'))]
        broker.dispatch('submit_analysis_review', review_payload(broker.draft_sha256, items, fresh))
        merged = reviewed_report(draft, broker.report)
        self.assertEqual(canonical_bytes(draft), original)
        self.assertEqual([x['review']['verdict'] for x in merged['findings']], ['supported', 'rejected', 'needs_verification'])
        self.assertEqual(merged['findings'][1]['title'], draft['findings'][1]['title'])
        self.assertFalse(merged['tests_executed'])

    def completion_script(self, fail_review=None, empty=False):
        counts = {'analyst': 0, 'reviewer': 0}
        digest = None
        def complete(kwargs):
            nonlocal digest
            tools = {item['function']['name'] for item in kwargs['tools']}
            phase = 'reviewer' if 'submit_analysis_review' in tools else 'analyst'
            counts[phase] += 1
            ordinal = counts[phase]
            if phase == 'analyst':
                if ordinal == 1: return tool_response('read_source', {'path': 'app.py', 'start': 1, 'end': 2})
                evidence_id = json.loads(kwargs['messages'][-1]['content'])['evidence_id']
                value = report(evidence_id)
                if empty: value['findings'] = []
                return tool_response('submit_analysis', value)
            if fail_review: return fail_review(kwargs)
            task = json.loads(kwargs['messages'][1]['content'])
            digest = task['draft_sha256']
            if ordinal == 1: return tool_response('read_draft', {'section': 'overview'})
            if not empty and ordinal == 2: return tool_response('read_draft', {'section': 'finding', 'finding_id': 'F001'})
            if ordinal == (2 if empty else 3): return tool_response('read_source', {'path': 'app.py', 'start': 1, 'end': 2})
            fresh = next(json.loads(message['content'])['evidence_id'] for message in reversed(kwargs['messages'])
                         if message['role'] == 'tool' and 'evidence_id' in json.loads(message['content']))
            return tool_response('submit_analysis_review', review_payload(digest, [] if empty else [assessment('F001', fresh)], fresh))
        return complete

    def test_two_actual_model_instances_have_separate_contexts_and_attributed_shared_usage(self):
        row, calls, budget = self.scripted_run(self.row(), self.completion_script())
        self.assertEqual(row['status'], 'finished')
        self.assertEqual(row['review_status'], 'completed')
        self.assertEqual(row['agent_exit'], 'AnalysisReviewed')
        self.assertNotEqual(row['analysis_roles']['analyst']['session_id'], row['analysis_roles']['reviewer']['session_id'])
        self.assertEqual(row['metrics']['requests'], 6)
        self.assertEqual(len(json.loads(budget.path.read_bytes())['requests']), 6)
        reservations = [event for event in self.store.events(row['id']) if event['kind'] == 'analysis_role_usage_reserved']
        self.assertEqual([event['data']['role'] for event in reservations], ['analyst'] * 2 + ['reviewer'] * 4)
        reviewer_first = calls[2]
        self.assertEqual(len(reviewer_first['messages']), 3)  # Two initial messages plus mechanical read receipt.
        self.assertEqual(json.loads(reviewer_first['messages'][-1]['content'])['program_review_state']['recorded_findings'], [])
        self.assertIn('NEW session', reviewer_first['messages'][0]['content'])
        self.assertNotIn('assistant', [m['role'] for m in reviewer_first['messages']])
        self.assertEqual({item['function']['name'] for item in reviewer_first['tools']}, set(REVIEW_SCHEMAS))
        draft = json.loads(registered_artifact(self.store, row, 'draft-report.json'))
        self.assertEqual(sha256(canonical_bytes(draft)), row['draft_sha256'])
        final = json.loads(registered_artifact(self.store, row, 'report.json'))
        self.assertEqual(final['review']['draft_sha256'], row['draft_sha256'])
        text = registered_artifact(self.store, row, 'report.txt').decode()
        self.assertIn('反例与替代解释', text)
        self.assertIn('复核结论', text)
        self.assertEqual((self.store.home / 'sources' / self.source['id'] / 'files/app.py').read_bytes(), self.raw)

    def test_empty_findings_still_get_fresh_source_and_overview_review(self):
        row, calls, _ = self.scripted_run(self.row(), self.completion_script(empty=True))
        self.assertEqual(row['status'], 'finished')
        value = json.loads(registered_artifact(self.store, row, 'report.json'))
        self.assertEqual(value['review']['assessments'], [])
        self.assertEqual(value['findings'], [])
        self.assertEqual(len(value['review']['evidence']), 1)

    def test_reviewer_auth_failure_keeps_immutable_draft_and_does_not_claim_reviewed(self):
        def fail(kwargs):
            error = RuntimeError('primary-test-secret failed authentication')
            error.status_code = 401
            raise error
        row, calls, _ = self.scripted_run(self.row(), self.completion_script(fail_review=fail))
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['review_status'], 'failed')
        self.assertIn('draft-report.json', row['artifacts'])
        self.assertNotIn('report.json', row['artifacts'])
        self.assertEqual(len(calls), 4)  # Gateway and official each once, per the existing route policy.
        self.assertEqual(row['metrics']['requests'], 4)
        self.assertNotIn(b'primary-test-secret', registered_artifact(self.store, row, 'reviewer-request_error.json'))

    def test_cancel_at_handoff_preserves_draft_and_never_starts_reviewer(self):
        row = self.row()
        original_update = self.store.update
        def update(run_id, **kwargs):
            result = original_update(run_id, **kwargs)
            if kwargs.get('event') == 'analysis_draft_frozen': self.store.cancel(run_id)
            return result
        with patch.object(self.store, 'update', side_effect=update):
            ended, calls, _ = self.scripted_run(row, self.completion_script())
        self.assertEqual(ended['status'], 'cancelled')
        self.assertEqual(len(calls), 2)
        self.assertIn('draft-report.json', ended['artifacts'])
        self.assertNotIn('report.json', ended['artifacts'])
        self.assertNotIn('reviewer', ended['analysis_roles'])

    def test_cancel_during_reviewer_retains_received_usage_and_unreviewed_draft(self):
        row = self.row()
        def cancel(kwargs):
            self.store.cancel(row['id'])
            return tool_response('read_draft', {'section': 'overview'})
        ended, calls, _ = self.scripted_run(row, self.completion_script(fail_review=cancel))
        self.assertEqual(ended['status'], 'cancelled')
        self.assertEqual(ended['metrics']['requests'], 3)
        self.assertEqual(ended['review_status'], 'cancelled')
        self.assertNotIn('report.json', ended['artifacts'])
        self.assertIn('draft-report.json', ended['artifacts'])

    def test_reviewer_deadline_stops_without_new_chance(self):
        clock = [1000.0]
        def expire(kwargs):
            clock[0] = 2000.0
            return tool_response('read_draft', {'section': 'overview'})
        with patch('analysis_execution.time.monotonic', side_effect=lambda: clock[0]):
            ended, calls, _ = self.scripted_run(self.row(), self.completion_script(fail_review=expire))
        self.assertEqual(ended['status'], 'failed')
        self.assertEqual(len(calls), 3)
        self.assertNotIn('report.json', ended['artifacts'])
        self.assertIn('draft-report.json', ended['artifacts'])

    def test_draft_tamper_at_handoff_blocks_second_model_and_all_publication(self):
        row = self.row()
        original_update = self.store.update
        def update(run_id, **kwargs):
            result = original_update(run_id, **kwargs)
            if kwargs.get('event') == 'analysis_draft_frozen':
                path = self.store.home / 'runs' / run_id / 'draft-report.json'
                path.write_bytes(path.read_bytes() + b' ')
            return result
        with patch.object(self.store, 'update', side_effect=update):
            ended, calls, _ = self.scripted_run(row, self.completion_script())
        self.assertEqual(ended['agent_exit'], 'SourceIntegrityFailed')
        self.assertEqual(len(calls), 2)
        self.assertEqual(ended['artifacts'], {})

    def test_restart_marks_review_interrupted_without_repeating_or_losing_draft(self):
        row = self.row()
        draft = self.store.home / 'draft-retained.json'; draft.write_bytes(b'fixture draft')
        self.store.update(row['id'], status='running', stage='reviewing_analysis', draft_sha256='a' * 64, review_status='running',
            analysis_roles={'analyst': {'status': 'submitted'}, 'reviewer': {'status': 'running'}}, artifacts={'draft-report.json': str(draft)})
        self.store.reconcile()
        ended = self.store.get(row['id'])
        self.assertEqual(ended['status'], 'interrupted')
        self.assertEqual(ended['review_status'], 'interrupted')
        self.assertEqual(ended['analysis_roles']['reviewer']['status'], 'interrupted')
        self.assertEqual(ended['analysis_roles']['analyst']['status'], 'submitted')
        self.assertEqual(draft.read_bytes(), b'fixture draft')
        self.assertFalse(any(item['status'] == 'queued' for item in self.store.list()))

    def test_incremental_assessments_preserve_twelve_findings_without_one_large_model_output(self):
        draft, broker = self.broker(count=12)
        self.read_draft(broker, 'overview')
        fresh = broker.span('app.py', 1, 2)['evidence_id']
        for index in range(1, 13):
            identity = f'F{index:03d}'
            self.read_draft(broker, 'finding', identity)
            item = assessment(identity, fresh)
            broker.dispatch('record_finding_review', item)
            broker.dispatch('record_finding_review', item)  # Exact retry is idempotent.
        self.assertEqual(broker.request_state()['pending_findings'], [])
        self.assertEqual(len(broker.recorded), 12)
        final = review_payload(broker.draft_sha256, [], fresh)
        broker.dispatch('submit_analysis_review', final)
        self.assertEqual(len(broker.report['assessments']), 12)
        self.assertEqual(len(reviewed_report(draft, broker.report)['findings']), 12)
        with self.assertRaises(ValueError):
            broker.dispatch('record_finding_review', assessment('F001', fresh, 'rejected'))

    def test_pinned_receipt_survives_omitted_read_bodies_and_supports_incremental_submission(self):
        calls = {'analyst': 0, 'reviewer': 0}
        def complete(kwargs):
            reviewer = 'submit_analysis_review' in {tool['function']['name'] for tool in kwargs['tools']}
            phase = 'reviewer' if reviewer else 'analyst'; calls[phase] += 1
            if not reviewer:
                if calls[phase] == 1: return tool_response('read_source', {'path': 'app.py', 'start': 1, 'end': 2})
                fresh = json.loads(kwargs['messages'][-1]['content'])['evidence_id']
                return tool_response('submit_analysis', report(fresh))
            state = json.loads(kwargs['messages'][-1]['content'])['program_review_state']
            if not state['overview_read']: return tool_response('read_draft', {'section': 'overview'})
            if 'F001' not in state['fully_read_findings']: return tool_response('read_draft', {'section': 'finding', 'finding_id': 'F001'})
            if not state['recent_source_evidence']: return tool_response('read_source', {'path': 'app.py', 'start': 1, 'end': 2})
            evidence_id = state['recent_source_evidence'][0]['evidence_id']
            if 'F001' not in state['recorded_findings']: return tool_response('record_finding_review', assessment('F001', evidence_id))
            self.assertEqual(state['pending_findings'], [])
            return tool_response('submit_analysis_review', review_payload(state['draft_sha256'], [], evidence_id))
        row, wire, _ = self.scripted_run(self.row(), complete)
        self.assertEqual(row['status'], 'finished')
        self.assertEqual(len(wire), 7)
        self.assertEqual(wire[-1]['messages'][-1]['role'], 'user')
        partial = json.loads(registered_artifact(self.store, row, 'review-progress.json'))
        self.assertEqual(partial['status'], 'partial')  # Never labels a partial file as completed.
        self.assertEqual(len(partial['assessments']), 1)

    def test_repeated_unchanged_reads_stop_before_more_paid_requests_without_global_step_cap(self):
        def repeat(kwargs): return tool_response('read_source', {'path': 'app.py', 'start': 1, 'end': 2})
        row, calls, _ = self.scripted_run(self.row(), self.completion_script(fail_review=repeat))
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(len(calls), 11)  # Analyst 2 + first fresh reviewer read + 8 no-progress actions.
        self.assertIn('8', Path(row['artifacts']['diagnostic.txt']).read_text(encoding='utf8'))
        self.assertNotIn('report.json', row['artifacts'])

    def test_read_receipt_and_evidence_index_survive_real_api_view_compaction(self):
        from analysis_sources import import_source
        self.raw = b'def divide(a, b):\n    return a / b\n' + (b'# ' + b'x' * 600 + b'\n') * 30
        self.source = import_source(self.store.home, 'app.py', self.raw)
        counters = {'analyst': 0}; picked = []
        def complete(kwargs):
            names = {tool['function']['name'] for tool in kwargs['tools']}
            if 'submit_analysis_review' not in names:
                counters['analyst'] += 1
                if counters['analyst'] == 1: return tool_response('read_source', {'path': 'app.py', 'start': 1, 'end': 2})
                return tool_response('submit_analysis', report(json.loads(kwargs['messages'][-1]['content'])['evidence_id']))
            state = json.loads(kwargs['messages'][-1]['content'])['program_review_state']
            if not state['overview_read']: return tool_response('read_draft', {'section': 'overview'})
            if not state['fully_read_findings']: return tool_response('read_draft', {'section': 'finding', 'finding_id': 'F001'})
            if state['evidence_total'] < 10:
                start = state['evidence_total'] * 2 + 1
                return tool_response('read_source', {'path': 'app.py', 'start': start, 'end': start + 1})
            if not picked:
                receipt = json.loads(next(message['content'] for message in reversed(kwargs['messages']) if message['role'] == 'tool'))
                if 'evidence' not in receipt: return tool_response('list_review_evidence', {'offset': 0})
                picked.append(receipt['evidence'][0]['evidence_id'])
            if not state['recorded_findings']:
                return tool_response('record_finding_review', assessment('F001', picked[0]))
            return tool_response('submit_analysis_review', review_payload(state['draft_sha256'], [], picked[0]))
        row, calls, _ = self.scripted_run(self.row(), complete)
        self.assertEqual(row['status'], 'finished')
        trajectory = json.loads(registered_artifact(self.store, row, 'reviewer-trajectory.json'))
        self.assertGreater(len(trajectory['messages']), len(calls[-1]['messages']))
        last_state = json.loads(calls[-1]['messages'][-1]['content'])['program_review_state']
        self.assertEqual(last_state['evidence_total'], 10)
        self.assertEqual(last_state['recorded_findings'], ['F001'])
        self.assertTrue(last_state['overview_read'])
        self.assertLessEqual(len(canonical_bytes(calls[-1]['messages'])), 16384)


if __name__ == '__main__':
    unittest.main(verbosity=2)
