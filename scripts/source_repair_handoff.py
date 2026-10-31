"""Read frozen analysis reasoning during repair without treating it as truth."""
import copy

from collaboration_protocol import Message, Identifier, canonical_bytes


class ReadAnalysisFinding(Message):
    finding_id: Identifier


class AnalysisHandoff:
    def __init__(self, analysis, workspace):
        self.analysis, self.workspace = analysis, workspace
        self.findings = {item['finding_id']: item for item in analysis['findings']}

    def index(self):
        return [{'finding_id': key, 'title_preview': item['title'][:80], 'title_truncated': len(item['title']) > 80,
                 'severity': item['severity'], 'review_verdict': item.get('review', {}).get('verdict', 'not_reviewed'),
                 'review_category': item.get('review', {}).get('category', 'uncertain'),
                 'review_execution': item.get('review_execution', {}).get('execution_status', 'not_recorded')}
                for key, item in self.findings.items()]

    def read(self, finding_id):
        self.workspace.verify()
        if finding_id not in self.findings: raise ValueError('原分析中没有这个问题身份。')
        item = copy.deepcopy(self.findings[finding_id])
        item.pop('review_execution', None)  # Its assessment duplicates item.review; index retains execution status.
        locations = []
        for identity in item['evidence_ids']:
            source = self.analysis['evidence'][identity]
            if source['path'] not in self.workspace.original: raise ValueError('分析依据超出本次修复范围。')
            locations.append({key: source[key] for key in ('path', 'start', 'end', 'file_sha256')})
        value = {'finding': item, 'original_locations': locations,
                 'note': 'This is the frozen analyst candidate and its actual independent review, if any. '
                         'Rejected, unreviewed or uncertain findings are NOT confirmed defects. '
                         'Original locations refer to the original file hashes; read_source reads current code, whose lines may have moved. '
                         'Decide relevance to the whole user goal and inspect current source; do not blindly apply suggestions.'}
        if len(canonical_bytes(value)) > 9000:
            raise ValueError('本条完整分析超过材料限额，未截断或冒充已读；请保留不确定性。')
        return value
