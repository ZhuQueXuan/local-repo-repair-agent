"""Consolidate issued source reads without dropping evidence or repeating bodies."""

def analyst_material(broker):
    by_path = {}
    bindings = []
    for ref in broker.evidence.values():
        bindings.append({key: value for key, value in ref.items() if key != 'content'})
        lines = by_path.setdefault(ref['path'], {})
        for number, text in enumerate(ref['content'].split('\n'), ref['start']):
            if number in lines and lines[number] != text:
                raise ValueError('分析材料同一行内容不一致。')
            lines[number] = text
    material, incomplete, missing = [], [], []
    for path in broker.paths:
        lines = by_path.get(path, {})
        if len(lines) < broker.source['files'][path]['lines']: incomplete.append(path)
        first = None
        for number in range(1, broker.source['files'][path]['lines'] + 2):
            absent = number <= broker.source['files'][path]['lines'] and number not in lines
            if absent and first is None: first = number
            if first is not None and (not absent or number - first == 79):
                end = number if absent else number - 1
                missing.append({'path': path, 'start': first, 'end': end})
                first = None
        start, group = None, []
        for number in sorted(lines):
            if group and number != start + len(group):
                material.append({'path': path, 'start': start, 'end': start + len(group) - 1, 'content': '\n'.join(group)})
                group = []
            if not group: start = number
            group.append(lines[number])
        if group: material.append({'path': path, 'start': start, 'end': start + len(group) - 1, 'content': '\n'.join(group)})
    return {'source_material': material, 'evidence_bindings': bindings,
            'incomplete_files': incomplete, 'missing_ranges': missing[:24],
            'missing_range_count': len(missing), 'missing_ranges_incomplete': len(missing) > 24,
            'note': 'All issued source lines remain visible here, merged without duplicates. Cite evidence_bindings IDs. Do not reread displayed ranges; inspect only missing material or submit the focused report with honest coverage limits.'}
