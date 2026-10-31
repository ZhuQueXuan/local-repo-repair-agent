"""Review a frozen analysis in a fresh read-only session, with fresh source evidence."""
import json
from typing import Annotated, Literal

from pydantic import Field
from analysis_runtime import AnalysisBroker, ListSourceFiles, ReadSource, SearchSource
from collaboration_protocol import Message, Identifier, Digest, Text, ShortText, canonical_bytes, sha256
from collaboration_panel import checked_bytes


class ListDraftFindings(Message):
    offset: Annotated[int, Field(ge=0, le=12)] = 0


class ListReviewEvidence(Message):
    offset: Annotated[int, Field(ge=0, le=100000)] = 0


class ReadDraft(Message):
    section: Literal['overview', 'structure', 'flow', 'unknowns', 'finding']
    finding_id: Identifier | None = None
    offset: Annotated[int, Field(ge=0, le=40000)] = 0
    limit: Annotated[int, Field(ge=1, le=800)] = 800


class FindingAssessment(Message):
    finding_id: Identifier
    verdict: Literal['supported', 'rejected', 'needs_verification']
    category: Literal['defect_candidate', 'improvement', 'uncertain']
    reason: Text
    counterargument: Text
    conditions: Text
    recommendation: Text
    evidence_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=6)]


class SubmitAnalysisReview(Message):
    draft_sha256: Digest
    summary: Text
    overview_assessment: Text
    assessments: Annotated[tuple[FindingAssessment, ...], Field(max_length=12)] = ()
    unknowns: Annotated[tuple[ShortText, ...], Field(min_length=1, max_length=12)]
    evidence_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=12)]


REVIEW_SCHEMAS = {'list_source_files': ListSourceFiles, 'read_source': ReadSource, 'search_source': SearchSource,
                  'list_draft_findings': ListDraftFindings, 'read_draft': ReadDraft, 'list_review_evidence': ListReviewEvidence,
                  'record_finding_review': FindingAssessment, 'submit_analysis_review': SubmitAnalysisReview}


class AnalysisReviewBroker(AnalysisBroker):
    def __init__(self, home, row, output, draft_sha256, *, check=lambda: None):
        super().__init__(home, row, check=check)
        self.output, self.draft_sha256 = output, draft_sha256
        self.draft = self.load_draft()
        self.findings = {finding['finding_id']: finding for finding in self.draft['findings']}
        if len(self.findings) != len(self.draft['findings']):
            raise ValueError('初稿问题身份重复。')
        self.read_sections = set()
        self.read_findings = set()
        self.read_overview = False
        self.recorded = {}

    def evidence_index(self):
        return [{key: ref[key] for key in ('evidence_id', 'path', 'start', 'end')} for ref in self.evidence.values()]

    def request_state(self):
        """Mechanical receipts, not a generated summary or extra authority."""
        pending = [key for key in self.findings if key not in self.recorded]
        evidence = self.evidence_index()
        value = {'draft_sha256': self.draft_sha256, 'overview_read': self.read_overview,
                 'fully_read_findings': sorted(self.read_findings), 'recorded_findings': sorted(self.recorded),
                 'pending_findings': pending, 'next_finding': pending[0] if pending else None,
                 'evidence_total': len(evidence), 'recent_source_evidence': evidence[-3:],
                 'evidence_index_tool': 'list_review_evidence',
                 'note': '程序只记录已发生的读取和逐条回执，不代表结论正确；原文仍用read_draft/read_source读取。'}
        while len(canonical_bytes(value)) > 3000 and value['recent_source_evidence']:
            value['recent_source_evidence'].pop(0)
        return value

    def progress_signature(self):
        def covered(intervals):
            total, stop = 0, 0
            for start, end in sorted(intervals):
                if end > stop:
                    total += end - max(start, stop)
                    stop = end
            return total
        sections = {key for key, _, _ in self.read_sections}
        draft_chars = sum(covered([(start, end) for section, start, end in self.read_sections if section == key]) for key in sections)
        paths = {ref['path'] for ref in self.evidence.values()}
        source_lines = sum(covered([(ref['start'] - 1, ref['end']) for ref in self.evidence.values() if ref['path'] == path]) for path in paths)
        return draft_chars, source_lines, len(self.recorded)

    def validate_assessment(self, item):
        if item.finding_id not in self.findings or item.finding_id not in self.read_findings:
            raise ValueError('必须先完整读取本条原始问题。')
        if not set(item.evidence_ids) <= set(self.evidence):
            raise ValueError('本条复核只能引用本会话亲自读取的rv-证据。')
        for evidence_id in item.evidence_ids:
            ref = self.evidence[evidence_id]
            if self.span(ref['path'], ref['start'], ref['end']) != ref:
                raise ValueError('本条源码依据已改变。')

    def load_draft(self):
        self.check()
        raw = checked_bytes(self.output, 'draft-report.json', limit=1024 * 1024)
        if sha256(raw) != self.draft_sha256:
            raise ValueError('初稿已改变，不能复核另一个版本。')
        value = json.loads(raw)
        if value['source_id'] != self.row['source_id'] or value['source_sha256'] != self.row['source_sha256']:
            raise ValueError('初稿与代码来源不一致。')
        return value

    def span(self, path, start, end):
        value = super().span(path, start, end)
        self.evidence.pop(value['evidence_id'])
        value['evidence_id'] = 'rv-' + value['evidence_id'][3:]
        self.evidence[value['evidence_id']] = value
        return value

    def dispatch(self, name, arguments):
        self.check()
        self.load_draft()
        if name not in REVIEW_SCHEMAS:
            raise ValueError('复核只允许登记的读取与复核提交工具。')
        value = REVIEW_SCHEMAS[name].parse(arguments)
        if name in {'list_source_files', 'read_source', 'search_source'}:
            return super().dispatch(name, arguments)
        if name == 'list_review_evidence':
            refs, size = [], 0
            values = self.evidence_index()
            for ref in values[value.offset:]:
                size += len(canonical_bytes(ref))
                if refs and size > 2600:
                    break
                refs.append(ref)
                if len(refs) >= 4:
                    break
            end = value.offset + len(refs)
            return {'evidence': refs, 'total': len(values), 'next_offset': end if end < len(values) else None}
        if name == 'record_finding_review':
            self.validate_assessment(value)
            item = value.model_dump(mode='json')
            previous = self.recorded.get(value.finding_id)
            if previous is not None and previous != item:
                raise ValueError('本条已提交不同结论，不能覆盖；保留原回执。')
            self.recorded[value.finding_id] = item
            return {'status': 'recorded', 'finding_id': value.finding_id, 'state': self.request_state()}
        if name == 'list_draft_findings':
            values = list(self.findings.values())[value.offset:value.offset + 4]
            end = value.offset + len(values)
            return {'draft_sha256': self.draft_sha256, 'findings': [
                {'finding_id': item['finding_id'], 'title_preview': item['title'][:80], 'title_truncated': len(item['title']) > 80,
                 'severity': item['severity']} for item in values],
                'total': len(self.findings), 'next_offset': end if end < len(self.findings) else None}
        if name == 'read_draft':
            if value.section == 'finding':
                if value.finding_id not in self.findings:
                    raise ValueError('初稿中没有该问题身份。')
                item = self.findings[value.finding_id]
                refs = [self.draft['evidence'][key] for key in item['evidence_ids']]
                target = item | {'source_locations': [{key: ref[key] for key in ('path', 'start', 'end')} for ref in refs]}
            else:
                if value.finding_id is not None:
                    raise ValueError('概览段落不接受问题身份。')
                target = self.draft[value.section]
            text = target if isinstance(target, str) else canonical_bytes(target).decode()
            if value.offset >= len(text):
                raise ValueError('段落偏移超出原文。')
            end = min(len(text), value.offset + value.limit)
            # Keep explicit read intervals; a later page alone cannot certify a full read.
            section = (value.section, value.finding_id)
            self.read_sections.add((section, value.offset, end))
            intervals = sorted((start, stop) for key, start, stop in self.read_sections if key == section)
            covered = 0
            for start, stop in intervals:
                if start > covered:
                    break
                covered = max(covered, stop)
            if covered >= len(text):
                if value.section == 'finding':
                    self.read_findings.add(value.finding_id)
                elif value.section == 'overview':
                    self.read_overview = True
            return {'draft_sha256': self.draft_sha256, 'section': value.section, 'finding_id': value.finding_id,
                    'text': text[value.offset:end], 'offset': value.offset, 'end': end, 'total_characters': len(text),
                    'section_sha256': sha256(text.encode()), 'next_offset': end if end < len(text) else None,
                    'note': '初稿是待核对的模型判断；必须自己读取源码，不能把初稿引文直接当复核证据。'}
        provided = [item.finding_id for item in value.assessments]
        if len(provided) != len(set(provided)):
            raise ValueError('复核问题身份不能重复。')
        combined = dict(self.recorded)
        for item in value.assessments:
            parsed = item.model_dump(mode='json')
            if item.finding_id in combined and combined[item.finding_id] != parsed:
                raise ValueError('总报告不能覆盖已有逐条复核。')
            combined[item.finding_id] = parsed
        items = [FindingAssessment.parse(combined[key]) for key in self.findings if key in combined]
        ids = list(combined)
        if value.draft_sha256 != self.draft_sha256:
            raise ValueError('复核必须绑定本次展示的初稿摘要。')
        if len(set(ids)) != len(ids) or set(ids) != set(self.findings):
            raise ValueError('必须逐条复核全部初稿问题，身份不能遗漏、重复或新增。')
        if set(ids) - self.read_findings:
            raise ValueError('请先完整读取各条初稿问题及后续分页，再提交逐条复核。')
        if not self.read_overview:
            raise ValueError('请先完整读取初稿overview，再核对概览与流程。')
        evidence_ids = set(value.evidence_ids)
        for item in items:
            evidence_ids.update(item.evidence_ids)
        if not evidence_ids <= set(self.evidence):
            raise ValueError('复核只能引用本会话亲自读取的rv-证据，不能借用初稿引文。')
        for ref in list(self.evidence.values()):
            if self.span(ref['path'], ref['start'], ref['end']) != ref:
                raise ValueError('源码依据已改变。')
        self.report = value.model_dump(mode='json') | {'assessments': [item.model_dump(mode='json') for item in items],
            'status': 'completed', 'evidence': dict(self.evidence), 'source_id': self.row['source_id'],
            'source_sha256': self.row['source_sha256'], 'coverage': {
                'allowed_files': len(self.paths), 'quoted_files': len({ref['path'] for ref in self.evidence.values()}),
                'quoted_spans': len(self.evidence), 'all_files_read': False},
            'note': '另一会话按同一快照复核；使用相同模型路由，结论仍可能相关或出错，未运行测试或修改代码。'}
        return {'status': 'submitted', 'assessments': len(ids)}


def reviewed_report(draft, review):
    if sha256(canonical_bytes(draft)) != review['draft_sha256']:
        raise ValueError('复核与初稿版本不一致。')
    assessments = {value['finding_id']: value for value in review['assessments']}
    expected = {item['finding_id'] for item in draft['findings']}
    is_v2 = review.get('protocol') == 'readonly_analysis_review_v2'
    if len(assessments) != len(review['assessments']) or (not set(assessments) <= expected if is_v2 else set(assessments) != expected):
        raise ValueError('复核问题集合不一致。')
    cases = {}
    if is_v2:
        cases = {item['finding_id']: item for item in review['case_results']}
        if len(cases) != len(review['case_results']) or set(cases) != (expected or {'OVERVIEW'}):
            raise ValueError('逐条执行状态集合不一致。')
        if any((item['execution_status'] == 'submitted') != (key in assessments or key == 'OVERVIEW' and review.get('overview_review') is not None)
               for key, item in cases.items()):
            raise ValueError('执行状态不能伪造模型复核意见。')
    evidence = draft['evidence'] | review['evidence']
    return draft | {'findings': [item | ({'review': assessments[item['finding_id']]} if item['finding_id'] in assessments else {}) |
                                ({'review_execution': cases[item['finding_id']]} if is_v2 else {}) for item in draft['findings']],
        'evidence': evidence, 'review': review, 'review_sha256': sha256(canonical_bytes(review)),
        'coverage': {'allowed_files': draft['coverage']['allowed_files'],
                     'quoted_files': len({ref['path'] for ref in evidence.values()}), 'quoted_spans': len(evidence), 'all_files_read': False},
        'note': ('分析初稿已提交；逐条复核状态与模型意见分别展示，未完成项没有模型结论。' if is_v2 and review['status'] != 'completed' else
                 '分析与另一会话复核均已提交；支持/驳回/待验证分别保留。') + '只读模型判断，未执行项目/测试，未修改代码。'}
