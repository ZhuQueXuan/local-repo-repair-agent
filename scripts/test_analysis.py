"""User-source intake, provenance, read-only model wire and real loopback HTTP.

All model responses and ledgers here are synthetic, with no provider requests.
Fixtures are retained under temp/; no imported code is executed.
"""
import io
import json
import os
from pathlib import Path
import stat
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from analysis_sources import import_source, load_source, source_bytes, fetch_github, validate_github, source_root
from analysis_runtime import AnalysisBroker, create_analysis, run_analysis
from collaboration_protocol import canonical_bytes
from collaboration_panel import registered_artifact
from deepseek_budget import SharedBudget, load_budget_config
from task_service_store import ROOT, RunStore
from task_service import make_handler
from litellm import ModelResponse


def zipped(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in files:
            archive.writestr(name, data)
    return stream.getvalue()


def tool_response(name, value):
    return ModelResponse(id='synthetic-response', model='synthetic-model', choices=[{'index': 0,
        'message': {'role': 'assistant', 'content': None, 'tool_calls': [{'id': uuid.uuid4().hex,
            'type': 'function', 'function': {'name': name, 'arguments': json.dumps(value)}}]}, 'finish_reason': 'tool_calls'}],
        usage={'prompt_tokens': 100, 'completion_tokens': 20})


def report(evidence_id):
    return {'overview': '开发夹具：除法函数。', 'structure': ['app.py：计算入口'], 'flow': ['入参进入除法运算。'],
        'findings': [{'title': '零分母引起异常', 'severity': 'medium', 'trigger': '调用divide(1, 0)',
            'impact': '抛出ZeroDivisionError，需调用方处理。', 'reasoning': '直接执行a / b，没有检查分母。',
            'suggestion': '定义零分母的合同，并在入口或调用处处理。', 'confidence': 'high', 'evidence_ids': [evidence_id]}],
        'unknowns': ['未执行测试；不知道调用方是否已处理异常。'], 'evidence_ids': [evidence_id]}


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.store = RunStore(ROOT / 'temp/analysis_tests' / uuid.uuid4().hex)
        self.raw = b'def divide(a, b):\r\n    return a / b\r\n'
        self.source = import_source(self.store.home, 'app.py', self.raw)

    def body(self, **overrides):
        return {'source_id': self.source['id'], 'source_sha256': self.source['manifest_sha256'], 'paths': ['app.py'],
            'focus': '梳理计算流程，指出明确的异常边界。', 'request_key': uuid.uuid4().hex,
            'external_processing_ack': True} | overrides

    def row(self, **overrides):
        return create_analysis(self.store, self.body(**overrides))

    def test_zip_filter_keeps_exact_bytes_and_never_executes_code(self):
        marker = self.store.home / 'must-not-run.txt'
        malicious = ('from pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("executed")\n').encode()
        value = import_source(self.store.home, 'project.zip', zipped([
            ('project/app.py', self.raw), ('project/payload.py', malicious), ('project/.env', b'confidential'),
            ('project/node_modules/package/a.js', b'ignored'), ('project/settings.py', b'password = "fixture-password"'),
            ('project/image.png', b'PNG'), ('project/gbk.txt', b'\xff\xfe'), ('project/key.txt', b'local-private-value')]), secrets=('local-private-value',))
        self.assertEqual(set(value['files']), {'app.py', 'payload.py'})
        self.assertEqual(source_bytes(self.store.home, value, 'app.py'), self.raw)
        self.assertEqual(len(value['excluded']), 6)
        self.assertFalse(marker.exists())
        self.assertNotIn(b'fixture-password', canonical_bytes(value))

    def test_zip_traversal_windows_aliases_case_collision_and_links_rejected(self):
        for paths in [[('../escape.py', b'x')], [('/absolute.py', b'x')],
                      [('NUL.py', b'x')], [('x.py', b'a'), ('X.py', b'b')], [('x./a.py', b'x')]]:
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                import_source(self.store.home, 'bad.zip', zipped(paths))
        link = zipfile.ZipInfo('link.py'); link.create_system = 3; link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(ValueError):
            import_source(self.store.home, 'link.zip', zipped([(link, b'outside.py')]))
        # ZipInfo normalizes Windows separators when creating an archive;
        # inject the original invalid spelling into both ZIP filename headers.
        with self.assertRaises(ValueError):
            import_source(self.store.home, 'backslash.zip', zipped([('x/file.py', b'x')]).replace(b'x/file.py', b'x\\file.py'))

    def test_intake_limits_and_no_eligible_source_are_clear(self):
        with patch('analysis_sources.MAX_UNPACKED', 10), self.assertRaises(ValueError):
            import_source(self.store.home, 'big.zip', zipped([('app.py', self.raw)]))
        value = import_source(self.store.home, 'big.zip', zipped([('app.py', self.raw), ('big.py', b'x' * 262145)]))
        self.assertEqual(set(value['files']), {'app.py'})
        with self.assertRaises(ValueError):
            import_source(self.store.home, '.env', b'secret')
        with self.assertRaises(ValueError):
            import_source(self.store.home, 'invalid.zip', b'not a zip')

    def test_public_github_url_validation_and_fixed_commit_archive(self):
        for url in ['http://github.com/a/b', 'https://github.com.evil/a/b', 'https://user:token@github.com/a/b',
                    'https://github.com/a/b?token=x', 'https://github.com/a/b/tree/main', 'https://127.0.0.1/a/b']:
            with self.subTest(url=url), self.assertRaises(ValueError): validate_github(url)
        sha = 'a' * 40
        payloads = iter([canonical_bytes({'sha': sha}), zipped([('owner-repo-sha/app.py', self.raw)])])
        urls = []
        class Opener:
            def open(self, request, **kwargs):
                urls.append(request.full_url)
                return io.BytesIO(next(payloads))
        with patch('analysis_sources.urllib.request.build_opener', return_value=Opener()):
            source = fetch_github(self.store.home, 'https://github.com/owner/repo.git', 'feature/a')
        self.assertTrue(urls[0].endswith('/commits/feature%2Fa'))
        self.assertTrue(urls[1].endswith('/zipball/' + sha))
        self.assertEqual(source['origin']['commit'], sha)
        self.assertEqual(source_bytes(self.store.home, source, 'app.py'), self.raw)

    def test_github_redirect_cannot_leave_allowlist(self):
        handlers = []
        def make(*args):
            handlers.extend(args)
            raise RuntimeError('captured handlers only')
        with patch('analysis_sources.urllib.request.build_opener', side_effect=make), self.assertRaises(RuntimeError):
            fetch_github(self.store.home, 'https://github.com/a/b')
        for url in ['http://codeload.github.com/a', 'https://localhost/a', 'https://api.github.com.evil/a', 'https://token@api.github.com/a']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                handlers[0].redirect_request(None, None, 302, '', {}, url)

    def test_ack_scope_and_idempotent_identity(self):
        body = self.body()
        first = create_analysis(self.store, body)
        self.assertEqual(first['id'], create_analysis(self.store, body)['id'])
        for changes in [{'focus': 'different'}, {'external_processing_ack': False}, {'paths': ['../app.py']}, {'paths': [[]]}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError): create_analysis(self.store, body | changes)
        self.assertEqual(len(self.store.list()), 1)

    def test_scope_and_forged_evidence_and_executable_tools_rejected(self):
        broker = AnalysisBroker(self.store.home, self.row())
        for name, args in [('bash', {'command': 'echo x'}), ('read_source', {'path': '../app.py', 'start': 1, 'end': 2}),
                           ('read_source', {'path': 'app.py', 'start': 0, 'end': 2}), ('submit_analysis', report('forged'))]:
            with self.subTest(tool=name), self.assertRaises(ValueError): broker.dispatch(name, args)
        ref = broker.dispatch('read_source', {'path': 'app.py', 'start': 1, 'end': 2})
        self.assertEqual(ref['content'], self.raw.decode().replace('\r\n', '\n').strip())
        broker.dispatch('submit_analysis', report(ref['evidence_id']))
        self.assertFalse(broker.report['tests_executed'])
        self.assertEqual(broker.report['coverage']['quoted_files'], 1)

    def test_literal_search_and_file_list_are_bounded_and_selected_only(self):
        source = import_source(self.store.home, 'files.zip', zipped([('app.py', self.raw), ('extra.py', b'return outside')]))
        row = self.row(source_id=source['id'], source_sha256=source['manifest_sha256'])
        broker = AnalysisBroker(self.store.home, row)
        self.assertEqual(broker.dispatch('list_source_files', {})['total_allowed'], 1)
        self.assertEqual(broker.dispatch('search_source', {'query': 'return'})['hits'][0]['path'], 'app.py')
        with self.assertRaises(ValueError): broker.content('extra.py')

    def test_tampered_source_or_manifest_invalidates_old_evidence(self):
        broker = AnalysisBroker(self.store.home, self.row())
        ref = broker.span('app.py', 1, 2)
        file = source_root(self.store.home, self.source['id']) / 'files/app.py'
        file.write_bytes(self.raw + b'# changed')
        with self.assertRaises(ValueError): broker.dispatch('submit_analysis', report(ref['evidence_id']))
        manifest = source_root(self.store.home, self.source['id']) / 'manifest.json'
        manifest.write_bytes(manifest.read_bytes() + b' ')
        with self.assertRaises(ValueError): load_source(self.store.home, self.source['id'], self.source['manifest_sha256'])

    def scripted_run(self, row, action=None):
        ledger = self.store.home / 'synthetic_shared.json'
        ledger.write_bytes(canonical_bytes({'historical_estimated_cost_cny': '0', 'historical_unknown_charge_reservation_cny': '0', 'blocked': False, 'requests': {}}))
        budget = SharedBudget(load_budget_config(), ledger_path=ledger, temp_dir=self.store.home / 'locks')
        settings = {'BUPT_API_KEY': 'primary-test-secret', 'BUPT_API_BASE_URL': 'https://myai.bupt.edu.cn/llm-gw/v1', 'BUPT_CHAT_MODEL': 'deepseek-v4-flash',
            'DEEPSEEK_API_KEY': 'backup-test-secret', 'DEEPSEEK_API_BASE_URL': 'https://api.deepseek.com', 'DEEPSEEK_CHAT_MODEL': 'deepseek-flash'}
        calls = []
        def complete(**kwargs):
            calls.append(kwargs)
            if action is not None: return action(kwargs)
            if len(calls) == 1: return tool_response('read_source', {'path': 'app.py', 'start': 1, 'end': 2})
            ref = json.loads(kwargs['messages'][-1]['content'])['evidence_id']
            return tool_response('submit_analysis', report(ref))
        with patch('dotenv.dotenv_values', return_value=settings), patch('task_service_worker.RunBudget', return_value=budget), patch('review_model.litellm.completion', side_effect=complete):
            run_analysis(self.store, row['id'])
        return self.store.get(row['id']), calls, budget

    def test_full_scripted_model_wire_report_download_and_accounting(self):
        row, calls, budget = self.scripted_run(self.row())
        self.assertEqual(row['status'], 'finished')
        self.assertEqual(row['metrics']['requests'], 2)
        self.assertEqual(len(json.loads(budget.path.read_bytes())['requests']), 2)
        self.assertEqual({t['function']['name'] for t in calls[0]['tools']}, {'read_source', 'search_source', 'submit_analysis'})
        result = json.loads(registered_artifact(self.store, row, 'report.json'))
        self.assertEqual(len(result['findings']), 1)
        self.assertIn('未执行', registered_artifact(self.store, row, 'report.txt').decode())
        self.assertEqual(source_bytes(self.store.home, self.source, 'app.py'), self.raw)
        Path(row['artifacts']['report.json']).write_bytes(b'tamper')
        with self.assertRaises(ValueError): registered_artifact(self.store, row, 'report.json')

    def test_long_source_chunks_remain_together_until_report_submission(self):
        raw = b''.join(('# source context line %03d; shared evidence for the whole module\n' % i).encode() for i in range(240))
        self.source = import_source(self.store.home, 'app.py', raw)
        seen = []
        def complete(kwargs):
            if len(seen) < 6:
                start = len(seen) * 40 + 1
                seen.append(start)
                return tool_response('read_source', {'path': 'app.py', 'start': start, 'end': start + 39})
            content = json.dumps(kwargs['messages'])
            self.assertIn('source context line 000', content)
            self.assertIn('source context line 239', content)
            self.assertNotIn('read_source', {tool['function']['name'] for tool in kwargs['tools']})
            ref = json.loads(kwargs['messages'][-1]['content'])['evidence_id']
            return tool_response('submit_analysis', report(ref))
        ended, calls, _ = self.scripted_run(self.row(), complete)
        self.assertEqual(ended['status'], 'finished', ended['reason'])
        self.assertEqual(len(calls), 7)

    def test_analyst_repeated_same_source_stops_on_no_new_coverage(self):
        def repeat(kwargs): return tool_response('read_source', {'path': 'app.py', 'start': 1, 'end': 2})
        ended, calls, _ = self.scripted_run(self.row(), repeat)
        self.assertEqual(ended['status'], 'failed')
        self.assertIn('连续8次', registered_artifact(self.store, ended, 'diagnostic.txt').decode())
        self.assertEqual(len(calls), 9)

    def test_analysis_material_identifies_missing_lines_between_reads(self):
        from analysis_material import analyst_material
        self.source = import_source(self.store.home, 'app.py', b'# line\n' * 200)
        broker = AnalysisBroker(self.store.home, self.row())
        broker.span('app.py', 1, 80)
        broker.span('app.py', 85, 160)
        broker.span('app.py', 161, 200)
        packet = analyst_material(broker)
        self.assertEqual(packet['missing_ranges'], [{'path': 'app.py', 'start': 81, 'end': 84}])
        self.assertEqual(packet['missing_range_count'], 1)

    def test_cancellation_after_response_keeps_usage_and_withholds_report(self):
        row = self.row()
        def cancel(kwargs):
            self.store.cancel(row['id'])
            return tool_response('read_source', {'path': 'app.py', 'start': 1, 'end': 2})
        ended, calls, _ = self.scripted_run(row, cancel)
        self.assertEqual(ended['status'], 'cancelled')
        self.assertEqual(ended['metrics']['requests'], 1)
        self.assertNotIn('report.json', ended['artifacts'])

    def test_queued_cancel_and_restart_never_create_a_new_chance(self):
        row = self.row(); self.store.cancel(row['id'])
        with patch('task_service_worker.RunBudget', side_effect=AssertionError('must not call')):
            run_analysis(self.store, row['id'])
        self.assertEqual(self.store.get(row['id'])['status'], 'cancelled')
        other = self.row(); self.store.update(other['id'], status='running')
        self.store.reconcile()
        self.assertEqual(self.store.get(other['id'])['status'], 'interrupted')

    def test_integrity_failure_before_model_is_terminal_without_report(self):
        row = self.row()
        manifest = source_root(self.store.home, self.source['id']) / 'manifest.json'
        manifest.write_bytes(manifest.read_bytes() + b' ')
        with patch('task_service_worker.RunBudget', side_effect=AssertionError('must not call')):
            run_analysis(self.store, row['id'])
        ended = self.store.get(row['id'])
        self.assertEqual(ended['status'], 'failed')
        self.assertEqual(ended['agent_exit'], 'SourceIntegrityFailed')
        self.assertNotIn('report.json', ended['artifacts'])


class AnalysisHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = RunStore(ROOT / 'temp/analysis_http_tests' / uuid.uuid4().hex)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
        cls.port = cls.server.server_port
        cls.server.RequestHandlerClass = make_handler(cls.store, 'offline-test-token', cls.port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(3)

    def request(self, path, body=None, token='offline-test-token'):
        raw = body if isinstance(body, bytes) else canonical_bytes(body) if body is not None else None
        request = urllib.request.Request('http://127.0.0.1:' + str(self.port) + path, data=raw, headers={'X-Task-Token': token})
        return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5)

    def test_main_and_original_three_modes_are_both_accessible(self):
        with self.request('/') as response: self.assertIn('公开GitHub'.encode(), response.read())
        with self.request('/experiments') as response:
            html = response.read().decode()
        self.assertIn('三角色', html)
        self.assertIn('双 Agent', html)
        self.assertIn('单 Agent', html)

    def test_real_binary_upload_preview_ack_duplicate_cancel_and_auth(self):
        with self.request('/api/sources/upload?name=app.py', b'def answer():\n    return 42\n') as response: source = json.load(response)
        with self.request('/api/sources/' + source['id'] + '?path=app.py') as response: self.assertIn('return 42', json.load(response)['content'])
        body = {'source_id': source['id'], 'source_sha256': source['manifest_sha256'], 'paths': ['app.py'], 'focus': '', 'request_key': uuid.uuid4().hex, 'external_processing_ack': True}
        for changes, token, expected in [({'external_processing_ack': False}, 'offline-test-token', 400), ({}, 'wrong', 403)]:
            with self.assertRaises(urllib.error.HTTPError) as error: self.request('/api/analysis', body | changes, token)
            self.assertEqual(error.exception.code, expected)
        with self.request('/api/analysis', body) as response: row = json.load(response)
        with self.request('/api/analysis', body) as response: self.assertEqual(json.load(response)['id'], row['id'])
        with self.request('/api/runs/' + row['id'] + '/cancel', {}) as response: self.assertEqual(json.load(response)['status'], 'cancelled')
        reviewed_body = body | {'analysis_mode': 'analysis_review', 'request_key': uuid.uuid4().hex}
        with self.request('/api/analysis', reviewed_body) as response: reviewed = json.load(response)
        self.assertEqual(reviewed['analysis_mode'], 'analysis_review')
        self.assertEqual(reviewed['analysis_policy']['parent_seconds'], 480)
        with self.request('/api/analysis', reviewed_body) as response: self.assertEqual(json.load(response)['id'], reviewed['id'])
        with self.assertRaises(urllib.error.HTTPError) as error: self.request('/api/analysis', reviewed_body | {'analysis_mode': 'single'})
        self.assertEqual(error.exception.code, 400)
        with self.request('/api/runs/' + reviewed['id'] + '/cancel', {}) as response: self.assertEqual(json.load(response)['status'], 'cancelled')


if __name__ == '__main__':
    unittest.main(verbosity=2)
