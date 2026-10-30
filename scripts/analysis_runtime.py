"""A source-bound, read-only analyst using the existing model route and ledger."""
import json
from pathlib import Path
from typing import Annotated, Literal
import uuid

from pydantic import Field
from collaboration_protocol import Message, Identifier, ShortText, Text, canonical_bytes, sha256
from analysis_sources import load_source, source_bytes
from task_service_store import now


class ListSourceFiles(Message):
    offset: Annotated[int, Field(ge=0, le=1000)] = 0


class ReadSource(Message):
    path: Annotated[str, Field(min_length=1, max_length=240)]
    start: Annotated[int, Field(ge=1)]
    end: Annotated[int, Field(ge=1)]


class SearchSource(Message):
    query: Annotated[str, Field(min_length=2, max_length=100)]
    offset: Annotated[int, Field(ge=0, le=5000)] = 0


class AnalysisFinding(Message):
    title: ShortText
    severity: Literal['high', 'medium', 'low']
    trigger: Text
    impact: Text
    reasoning: Text
    suggestion: Text
    confidence: Literal['low', 'medium', 'high']
    evidence_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=5)]


class SubmitAnalysis(Message):
    overview: Text
    structure: Annotated[tuple[ShortText, ...], Field(min_length=1, max_length=12)]
    flow: Annotated[tuple[ShortText, ...], Field(min_length=1, max_length=12)]
    findings: Annotated[tuple[AnalysisFinding, ...], Field(max_length=12)] = ()
    unknowns: Annotated[tuple[ShortText, ...], Field(min_length=1, max_length=12)]
    evidence_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=12)]


SCHEMAS = {'list_source_files': ListSourceFiles, 'read_source': ReadSource,
           'search_source': SearchSource, 'submit_analysis': SubmitAnalysis}


def create_analysis(store, body):
    required = {'source_id', 'source_sha256', 'paths', 'focus', 'request_key', 'external_processing_ack'}
    if (not isinstance(body, dict) or not required <= set(body) or set(body) - required - {'analysis_mode'}
            or body['external_processing_ack'] is not True):
        raise ValueError('请确认分析范围及模型外发说明后开始。')
    mode = body.get('analysis_mode', 'single')  # Preserve v1.1 clients and old idempotency keys.
    if mode not in ('single', 'analysis_review'):
        raise ValueError('请选择仅分析或分析后独立复核。')
    source = load_source(store.home, body['source_id'], body['source_sha256'])
    paths, focus, key = body['paths'], body['focus'], body['request_key']
    if (not isinstance(paths, list) or not paths or len(paths) > 1000
            or any(not isinstance(path, str) or path not in source['files'] for path in paths)
            or len(set(paths)) != len(paths)):
        raise ValueError('请选择快照中实际存在的文件。')
    if not isinstance(focus, str) or len(focus) > 1000 or not isinstance(key, str) or not 1 <= len(key) <= 120:
        raise ValueError('关注问题过长或请求身份无效。')
    fields = {'source_id': source['id'], 'source_sha256': source['manifest_sha256'], 'paths': sorted(paths), 'focus': focus.strip()}
    request_sha = sha256(canonical_bytes(fields | ({'analysis_mode': mode} if mode != 'single' else {})))
    from release_info import release_identity
    identity = release_identity()
    row = {'id': uuid.uuid4().hex, 'kind': 'analysis', 'task': source['name'],
           'profile': 'readonly_analysis_review_v1' if mode == 'analysis_review' else 'readonly_analysis_v1',
           'analysis_mode': mode, 'review_status': 'pending' if mode == 'analysis_review' else 'not_requested',
           'analysis_policy': {'analyst_seconds': 300, 'reviewer_seconds': 180 if mode == 'analysis_review' else 0,
                               'parent_seconds': 480 if mode == 'analysis_review' else 300},
           'status': 'queued', 'stage': 'queued', 'created': now(), 'updated': now(), 'cancel_requested': False,
           'agent_exit': None, 'repair_result': 'not_run', 'metrics': {}, 'artifacts': {}, 'reason': '',
           'scope': '只读代码分析，未运行测试或自动修复；代码依据不等于运行证实。',
           'runtime_identity': identity, 'request_sha256': request_sha, **fields}
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        found = db.execute('SELECT payload FROM runs WHERE request_key=?', (key,)).fetchone()
        if found:
            old = json.loads(found[0])
            if old.get('kind') != 'analysis' or old.get('request_sha256') != request_sha:
                raise ValueError('该请求身份已用于其他内容，请查看原任务。')
            return old
        db.execute('INSERT INTO runs VALUES(?,?,?,?,?)', (row['id'], key, 'queued', row['created'], canonical_bytes(row).decode()))
        store.emit(db, row['id'], 'analysis_created', {'source_id': source['id'], 'allowed_files': len(paths)})
    return row


class AnalysisBroker:
    def __init__(self, home, row, *, check=lambda: None):
        self.home, self.row, self.check = Path(home), row, check
        self.source = load_source(home, row['source_id'], row['source_sha256'])
        self.paths, self.evidence = sorted(row['paths']), {}
        self.report = None

    def content(self, path):
        self.check()
        load_source(self.home, self.source['id'], self.row['source_sha256'])
        if path not in self.paths:
            raise ValueError('文件不在本次允许范围。')
        return source_bytes(self.home, self.source, path).decode('utf-8-sig').splitlines()

    def span(self, path, start, end):
        lines = self.content(path)
        if end < start or end - start >= 80 or end > len(lines):
            raise ValueError(f'行号范围无效：该文件共{len(lines)}行，每次最多80行；使用start/end整数。')
        value = {'path': path, 'start': start, 'end': end, 'content': '\n'.join(lines[start-1:end]),
                 'file_sha256': self.source['files'][path]['sha256']}
        if len(canonical_bytes(value)) > 3300:
            raise ValueError('该行段过长，请缩小读取范围；超长单行可跳过并说明未知。')
        value['evidence_id'] = 'ev-' + sha256(canonical_bytes(value))[:32]
        self.evidence[value['evidence_id']] = value
        return value

    def dispatch(self, name, arguments):
        self.check()
        if name not in SCHEMAS:
            raise ValueError('只允许本次登记的只读工具。')
        value = SCHEMAS[name].parse(arguments)
        if name == 'list_source_files':
            result, size = [], 0
            for path in self.paths[value.offset:]:
                item = {'path': path, 'lines': self.source['files'][path]['lines']}
                size += len(canonical_bytes(item))
                if result and size > 2800:
                    break
                result.append(item)
                if len(result) >= 20:
                    break
            end = value.offset + len(result)
            return {'files': result, 'next_offset': end if end < len(self.paths) else None, 'total_allowed': len(self.paths)}
        if name == 'read_source':
            return self.span(value.path, value.start, value.end)
        if name == 'search_source':
            hits, seen, more = [], 0, False
            for path in self.paths:
                for number, line in enumerate(self.content(path), 1):
                    if value.query.casefold() not in line.casefold():
                        continue
                    seen += 1
                    if seen <= value.offset or len(line.encode()) > 1000:
                        continue
                    if len(hits) >= 3:
                        more = True
                        break
                    hits.append(self.span(path, number, number))
                if more:
                    break
            return {'hits': hits, 'next_offset': seen - 1 if more else None,
                    'note': '文字匹配不代表缺陷；超长匹配行未显示。'}
        ids = set(value.evidence_ids)
        for finding in value.findings:
            ids.update(finding.evidence_ids)
        if any(key not in self.evidence for key in ids):
            raise ValueError('报告只能引用本次已返回的evidence_id。')
        for ref in self.evidence.values():
            current = self.span(ref['path'], ref['start'], ref['end'])
            if current != ref:
                raise ValueError('引文对应源码已改变，不能提交旧报告。')
        self.report = value.model_dump(mode='json') | {
            'source_id': self.source['id'], 'source_sha256': self.row['source_sha256'], 'origin': self.source['origin'],
            'focus': self.row['focus'], 'evidence': self.evidence,
            'coverage': {'allowed_files': len(self.paths), 'quoted_files': len({ref['path'] for ref in self.evidence.values()}),
                         'quoted_spans': len(self.evidence), 'all_files_read': False},
            'validation_status': 'static_only_not_executed', 'tests_executed': False,
            'note': '模型分析与源码引用；未执行项目/测试，未修改代码，未经过第二角色复核。'}
        self.report['findings'] = [finding | {'finding_id': f'F{index:03d}'} for index, finding in enumerate(self.report['findings'], 1)]
        return {'status': 'submitted', 'findings': len(value.findings)}


def run_analysis(store, run_id, *, model_factory=None):
    from analysis_execution import execute_analysis
    return execute_analysis(store, run_id, model_factory=model_factory)


def interrupted_analysis_fields(row, status):
    if row.get('kind') != 'analysis':
        return {}
    roles = {key: dict(value) for key, value in row.get('analysis_roles', {}).items()}
    for role in roles.values():
        if role.get('status') == 'running':
            role.update(status=status, ended=now())
    fields = {'analysis_roles': roles}
    if row.get('analysis_mode') == 'analysis_review' and row.get('review_status') != 'completed':
        fields['review_status'] = status if row.get('draft_sha256') else 'not_started'
    return fields


def report_text(report):
    lines = [report['overview'], '', '项目结构', *report['structure'], '', '主要流程', *report['flow'], '', '发现的问题']
    review = report.get('review')
    if review:
        lines = ['分析＋独立会话复核（同一模型路由，未运行测试）', review['summary'], review['overview_assessment'], ''] + lines
    for finding in report['findings']:
        lines += ['', finding['severity'] + ' · ' + finding['title']]
        for title, key in [('触发条件', 'trigger'), ('影响', 'impact'), ('依据', 'reasoning'), ('建议', 'suggestion')]:
            lines.append(title + '：' + finding[key])
        lines.append('确定程度：' + finding['confidence'])
        for evidence_id in finding['evidence_ids']:
            ref = report['evidence'][evidence_id]
            lines.append(f"{ref['path']}:{ref['start']}—{ref['end']}")
        if finding.get('review'):
            assessment = finding['review']
            lines.append('复核结论：' + {'supported': '支持', 'rejected': '不支持', 'needs_verification': '待验证'}[assessment['verdict']])
            lines.append('分类：' + {'defect_candidate': '缺陷候选', 'improvement': '改进建议', 'uncertain': '仍不确定'}[assessment['category']])
            for title, key in [('核对理由', 'reason'), ('反例与替代解释', 'counterargument'), ('成立条件', 'conditions'), ('复核建议', 'recommendation')]:
                lines.append(title + '：' + assessment[key])
            for evidence_id in assessment['evidence_ids']:
                ref = report['evidence'][evidence_id]
                lines.append(f"复核原文 {ref['path']}:{ref['start']}—{ref['end']}")
    if not report['findings']:
        lines.append('本次没有提交具体缺陷；不代表整个项目没有问题。')
    lines += ['', '未知与待验证项', *report['unknowns']]
    if review:
        lines += ['', '复核后的未知项', *review['unknowns'], review['note']]
    lines += ['', report['note'], '允许文件数：' + str(report['coverage']['allowed_files']), '', '实际读取的源码片段']
    for ref in report['evidence'].values():
        lines += [f"{ref['path']}:{ref['start']}—{ref['end']}", ref['content'], '']
    return '\n'.join(lines)
