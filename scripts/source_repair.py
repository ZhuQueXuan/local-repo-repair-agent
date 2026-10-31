"""User-source repair records and immutable candidate workspaces; no execution."""
import difflib
import io
import json
from pathlib import Path
from typing import Annotated, Literal
import uuid
import zipfile

from pydantic import Field
from analysis_sources import load_source, source_bytes, SECRET, SKIP_DIRS, TEXT_SUFFIXES, MAX_FILE, MAX_TEXT, MAX_FILES
from collaboration_panel import registered_artifact, checked_bytes
from collaboration_protocol import Message, ShortText, Text, canonical_bytes, sha256, relative_path
from task_service_store import now

POLICY = {'max_rounds': 3, 'parent_seconds': 900, 'stagnant_rounds': 2, 'planning_seconds': 90,
          'repair_seconds': 150, 'check_seconds': 60, 'review_seconds': 60}


def create_source_repair(store, body):
    required = {'analysis_id', 'goal', 'request_key', 'external_processing_ack', 'execution_ack'}
    if (not isinstance(body, dict) or set(body) - required or not required <= set(body)
            or body['external_processing_ack'] is not True or body['execution_ack'] is not True):
        raise ValueError('请确认整体修复目标、模型处理及隔离运行许可。')
    goal, key = body['goal'], body['request_key']
    if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 2000 or not isinstance(key, str) or not 1 <= len(key) <= 120:
        raise ValueError('请用一句话说明修好后应达到什么效果。')
    parent = store.get(body['analysis_id'])
    if parent.get('kind') != 'analysis' or parent['status'] not in {'finished', 'failed', 'cancelled', 'interrupted'}:
        raise ValueError('请先完成或停止原分析，再启动独立修复任务。')
    name = 'report.json' if 'report.json' in parent['artifacts'] else 'draft-report.json'
    report = registered_artifact(store, parent, name)
    parsed = json.loads(report)
    if (parsed['source_id'], parsed['source_sha256']) != (parent['source_id'], parent['source_sha256']):
        raise ValueError('分析报告与冻结代码不一致。')
    source = load_source(store.home, parent['source_id'], parent['source_sha256'])
    selected = {path: source_bytes(store.home, source, path) for path in parent['paths']}
    fields = {'analysis_id': parent['id'], 'source_id': parent['source_id'], 'source_sha256': parent['source_sha256'],
              'paths': parent['paths'], 'goal': goal.strip(), 'analysis_report_sha256': sha256(report),
              'reference_mode': parent.get('reference_mode', 'off')}
    digest = sha256(canonical_bytes(fields))
    # Idempotency is checked BEFORE any environment operation; a repeated click
    # returns the original identity, including after the runtime becomes unavailable.
    with store.connect() as db:
        found = db.execute('SELECT payload FROM runs WHERE request_key=?', (key,)).fetchone()
    if found:
        old = json.loads(found[0])
        if old.get('kind') != 'source_repair' or old.get('request_sha256') != digest:
            raise ValueError('该请求身份已用于其他内容，请查看原任务。')
        return old
    from source_checks import require_runtime
    if any(path in {'_acceptance', '_check_tools', '_check_result.xml'} or path.startswith(('_acceptance/', '_check_tools/')) for path in selected):
        raise ValueError('项目路径与独立检查保留目录冲突，未创建修复任务或调用模型。')
    runtime = require_runtime(parent['paths'])
    from release_info import release_identity
    row = {'id': uuid.uuid4().hex, 'kind': 'source_repair', 'task': source['name'], 'profile': 'source_repair_v1',
           'status': 'queued', 'stage': 'queued', 'created': now(), 'updated': now(), 'cancel_requested': False,
           'agent_exit': None, 'repair_result': 'not_run', 'metrics': {}, 'artifacts': {}, 'reason': '',
           'repair_policy': POLICY, 'runtime': runtime, 'runtime_identity': release_identity(),
           'scope': '在冻结代码副本修改和隔离检查；不覆盖原项目。最多3轮、累计15分钟，模型判断仍需人工验收。',
           'request_sha256': digest, **fields}
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        found = db.execute('SELECT payload FROM runs WHERE request_key=?', (key,)).fetchone()
        if found:
            old = json.loads(found[0])
            if old.get('kind') != 'source_repair' or old.get('request_sha256') != digest: raise ValueError('请求身份冲突。')
            return old
        db.execute('INSERT INTO runs VALUES(?,?,?,?,?)', (row['id'], key, 'queued', row['created'], canonical_bytes(row).decode()))
        store.emit(db, row['id'], 'source_repair_created', {'analysis_id': parent['id'], 'policy': POLICY})
    return row


class EditSource(Message):
    path: Annotated[str, Field(min_length=1, max_length=240)]
    old: Annotated[str, Field(max_length=6000)]
    new: Annotated[str, Field(max_length=6000)]


class AddSource(Message):
    path: Annotated[str, Field(min_length=1, max_length=240)]
    content: Annotated[str, Field(min_length=1, max_length=6000)]


class SubmitChanges(Message):
    summary: Text
    remaining: Annotated[tuple[ShortText, ...], Field(max_length=8)] = ()


class AcceptanceFile(Message):
    path: Annotated[str, Field(pattern=r'^acceptance_[a-z0-9_]{1,40}\.(py|js)$')]
    content: Annotated[str, Field(min_length=1, max_length=6000)]


class SubmitAcceptance(Message):
    criteria: Annotated[tuple[ShortText, ...], Field(min_length=1, max_length=6)]
    tests: Annotated[tuple[AcceptanceFile, ...], Field(min_length=1, max_length=2)]
    limitations: Annotated[tuple[ShortText, ...], Field(max_length=6)] = ()


class SubmitRepairReview(Message):
    verdict: Literal['satisfied', 'needs_work', 'unclear']
    reason: Text
    blockers: Annotated[tuple[ShortText, ...], Field(max_length=8)] = ()
    unknowns: Annotated[tuple[ShortText, ...], Field(max_length=6)] = ()
    limitations: Annotated[tuple[ShortText, ...], Field(max_length=6)] = ()


def is_original_test(path):
    name = Path(path).name.lower()
    return (bool({'test', 'tests', '__tests__'} & {part.casefold() for part in Path(path).parts})
            or name.startswith('test') or name.endswith(('.test.js', '.spec.js', '_test.py', '.test.ts', '.spec.ts')))


class RepairWorkspace:
    def __init__(self, home, row, output):
        self.home, self.row, self.output = Path(home), row, Path(output)
        self.source = load_source(home, row['source_id'], row['source_sha256'])
        self.original = {path: source_bytes(home, self.source, path) for path in row['paths']}
        self.files = dict(self.original)
        self.snapshots, self.frozen_tests = [], {}
        self.edit_log = []

    def verify(self):
        source = load_source(self.home, self.row['source_id'], self.row['source_sha256'])
        for path, raw in self.original.items():
            if source_bytes(self.home, source, path) != raw: raise ValueError('原始快照已改变，停止修复。')

    def tree_sha(self):
        return sha256(canonical_bytes({path: sha256(raw) for path, raw in sorted(self.files.items())}))

    def edit(self, name, arguments):
        self.verify()
        value = (EditSource if name == 'edit_source' else AddSource).parse(arguments)
        path = relative_path(value.path)
        if (any(part.casefold() in SKIP_DIRS or part.startswith('.') for part in Path(path).parts)
                or Path(path).suffix.casefold() not in TEXT_SUFFIXES or is_original_test(path)
                or path.startswith('_acceptance/')):
            raise ValueError('不能修改原有测试、隐藏文件、运行数据或独立验收材料。')
        if name == 'edit_source':
            if path not in self.files: raise ValueError('编辑目标不在副本中。')
            text = self.files[path].decode('utf-8-sig')
            if not value.old:
                if text: raise ValueError('空原文只允许用于当前确实为空的文件。')
                content = value.new
            else:
                if text.count(value.old) != 1: raise ValueError('原文必须在当前文件中唯一匹配，请先读取准确内容。')
                content = text.replace(value.old, value.new, 1)
        else:
            if path in self.files or path.casefold() in {item.casefold() for item in self.files}:
                raise ValueError('新文件已存在或与现有路径冲突。')
            # New implementation files stay within a selected source directory.
            parents = {str(Path(item).parent).replace('\\', '/') for item in self.original if not is_original_test(item)}
            if str(Path(path).parent).replace('\\', '/') not in parents:
                raise ValueError('新文件必须位于本次已选择的代码目录，不扩展到其他目录。')
            content = value.content
        raw = content.encode('utf8')
        if not raw or len(raw) > MAX_FILE or SECRET.search(content): raise ValueError('修改为空、过大或含疑似凭据。')
        total = sum(len(data) for name, data in self.files.items() if name != path) + len(raw)
        if total > MAX_TEXT or len(self.files) + (path not in self.files) > MAX_FILES: raise ValueError('副本超过原导入范围的体积限制。')
        self.files[path] = raw
        receipt = {'path': path, 'file_sha256': sha256(raw), 'tree_sha256': self.tree_sha(), 'bytes': len(raw)}
        self.edit_log.append(receipt)
        return receipt

    def read(self, path, start=1, end=80):
        self.verify()
        if path not in self.files or type(start) is not int or type(end) is not int or start < 1 or end < start:
            raise ValueError('读取范围无效。')
        lines = self.files[path].decode('utf-8-sig').splitlines()
        end = min(end, len(lines), start + 99)
        text = '\n'.join(lines[start - 1:end])
        if len(text.encode()) > 6000: raise ValueError('行段过长，请选择更短原文。')
        return {'path': path, 'start': start, 'end': end, 'content': text, 'file_sha256': sha256(self.files[path]),
                'next_line': end + 1 if end < len(lines) else None}

    def seal(self, round_number):
        self.verify()
        name = f'candidate-{round_number:02d}'
        target = self.output / name
        target.mkdir(exist_ok=False)
        for path, raw in self.files.items():
            file = target / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(raw)
        diff = []
        for path in sorted(set(self.original) | set(self.files)):
            before = self.original.get(path, b'').decode('utf-8-sig').splitlines(keepends=True)
            after = self.files.get(path, b'').decode('utf-8-sig').splitlines(keepends=True)
            diff.extend(difflib.unified_diff(before, after, fromfile='a/' + path, tofile='b/' + path))
        patch = ''.join(diff).encode()
        (self.output / (name + '.diff')).write_bytes(patch)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as value:
            for path, raw in sorted(self.files.items()): value.writestr(path, raw)
        (self.output / (name + '.zip')).write_bytes(archive.getvalue())
        item = {'round': round_number, 'tree_sha256': self.tree_sha(), 'files': {p: sha256(raw) for p, raw in self.files.items()},
                'patch_sha256': sha256(patch), 'archive': name + '.zip', 'diff': name + '.diff',
                'changed': [p for p in self.files if self.files[p] != self.original.get(p)]}
        (self.output / (name + '-snapshot.json')).write_bytes(canonical_bytes(item))
        self.snapshots.append(item)
        return item

    def material(self, limit=8000):
        values, used = [], 0
        for path in sorted(self.files, key=lambda path: (is_original_test(path), path not in {e['path'] for e in self.edit_log}, path)):
            text = self.files[path].decode('utf-8-sig')
            value = {'path': path, 'content': text, 'file_sha256': sha256(self.files[path])}
            size = len(canonical_bytes(value))
            if used + size <= limit:
                values.append(value); used += size
        return {'files': values, 'provided_files': len(values), 'selected_files': len(self.files),
                'file_index': list(self.files)[:40], 'file_index_incomplete': len(self.files) > 40,
                'note': 'Only displayed files/ranges have been provided; use read_source for missing material.'}
