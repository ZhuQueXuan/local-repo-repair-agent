"""Trusted Python check driver, executed ONLY inside a disposable container.

The parent sends selected source and frozen acceptance files through stdin.
There is no host mount, network, Docker socket, model credential or package install.
"""
import ast
import base64
import json
from pathlib import Path
import sys
import os
import xml.etree.ElementTree as ET


def main():
    payload = json.load(sys.stdin)
    def emit(value):
        print('SOURCE_CHECK_RESULT_' + payload['receipt'] + '=' + json.dumps(value))
    root = Path('/work')
    root.mkdir(exist_ok=True)
    for name, data in payload['files'].items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data, validate=True))
    acceptance = root / '_acceptance'
    acceptance.mkdir(exist_ok=True)
    for name, data in payload['acceptance'].items():
        (acceptance / name).write_text(data, encoding='utf8')
    tools = root / '_check_tools'
    for name, data in payload['tools'].items():
        target = tools / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(data, validate=True))
    syntax, original_tests = [], []
    for name in payload['files']:
        if name.endswith('.py'):
            try:
                tree = ast.parse((root / name).read_bytes(), filename=name)
                path = Path(name)
                if path.name != '__init__.py' and ({'test', 'tests', '__tests__'} & set(path.parts) or path.name.startswith('test') or path.name.endswith('_test.py')):
                    original_tests.append(name)
            except SyntaxError as error: syntax.append({'path': name, 'line': error.lineno, 'reason': str(error)[:300]})
    if syntax:
        emit({'syntax_errors': syntax, 'tests': 0, 'errors': len(syntax), 'failures': 0, 'skipped': 0})
        return 1
    os.chdir(root)
    sys.path[:0] = [str(tools), str(root), str(root / 'src')]
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    os.environ['PY_COLORS'] = '0'
    report_path = root / '_check_result.xml'
    try:
        import pytest
        targets = sorted(original_tests) + [str(path) for path in acceptance.glob('acceptance_*.py')]
        if not targets:
            emit({'tests': 0, 'errors': 1, 'failures': 0, 'skipped': 0, 'reason': 'No test targets'})
            return 1
        code = int(pytest.main(['-q', '-p', 'no:cacheprovider', '--junitxml=' + str(report_path), *targets]))
        if not report_path.exists(): raise ValueError('No pytest program report')
        xml = ET.parse(report_path).getroot()
        suites = list(xml.iter('testsuite'))
        value = {key: sum(int(suite.attrib.get(key, 0)) for suite in suites) for key in ('tests', 'errors', 'failures', 'skipped')}
        value['syntax_errors'] = []
        emit(value)
        return 0 if code == 0 and value['tests'] > value['skipped'] and not value['errors'] and not value['failures'] else code or 1
    except Exception as error:
        emit({'tests': 0, 'errors': 1, 'failures': 0, 'skipped': 0, 'reason': type(error).__name__})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
