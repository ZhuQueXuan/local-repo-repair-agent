"""Keep current-version source reads visible across goal-driven phase actions."""
from collaboration_protocol import sha256


class SourceReadMemory:
    def __init__(self, workspace):
        self.workspace, self.lines = workspace, {}

    def prune(self):
        for key in list(self.lines):
            path, digest = key
            if path not in self.workspace.files or sha256(self.workspace.files[path]) != digest:
                del self.lines[key]

    def remember(self, receipt):
        self.prune()
        path, digest = receipt['path'], receipt['file_sha256']
        if sha256(self.workspace.files[path]) != digest:
            raise ValueError('读取回执不属于当前代码版本。')
        lines = self.lines.setdefault((path, digest), {})
        for number, text in enumerate(receipt['content'].split('\n'), receipt['start']):
            if number in lines and lines[number] != text:
                raise ValueError('当前源码同一行的读取内容不一致。')
            lines[number] = text

    def signature(self):
        self.prune()
        return tuple((path, digest, tuple(sorted(lines))) for (path, digest), lines in sorted(self.lines.items()))

    def material(self, base):
        self.prune()
        complete = {item['path'] for item in base['files']}
        ranges, missing = [], []
        for path, raw in self.workspace.files.items():
            if path in complete:
                continue
            digest = sha256(raw)
            lines = self.lines.get((path, digest), {})
            start, group = None, []
            for number in sorted(lines):
                if group and number != start + len(group):
                    ranges.append({'path': path, 'file_sha256': digest, 'start': start,
                                   'end': start + len(group) - 1, 'content': '\n'.join(group)})
                    group = []
                if not group: start = number
                group.append(lines[number])
            if group:
                ranges.append({'path': path, 'file_sha256': digest, 'start': start,
                               'end': start + len(group) - 1, 'content': '\n'.join(group)})
            first, size = None, 0
            source_lines = raw.decode('utf-8-sig').splitlines()
            count = len(source_lines)
            for number in range(1, count + 2):
                absent = number <= count and number not in lines
                line_size = len(source_lines[number - 1].encode()) if absent else 0
                if first is not None and (not absent or number - first >= 100 or size + 1 + line_size > 6000):
                    missing.append({'path': path, 'start': first, 'end': number - 1})
                    first, size = None, 0
                if absent:
                    if first is None: first = number
                    size += line_size + (1 if number > first else 0)
        return base | {'read_ranges': ranges, 'missing_ranges': missing[:24],
                       'missing_range_count': len(missing), 'missing_ranges_incomplete': len(missing) > 24,
                       'note': 'files are full current files; read_ranges retain all current-version reads, merged without duplicates. Read only missing_ranges. Changed file versions invalidate old reads.'}
