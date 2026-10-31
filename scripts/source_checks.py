"""Disposable, networkless Python/Node checks. Never execute uploaded code on host."""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid

from collaboration_protocol import canonical_bytes, sha256
from task_service_store import ROOT

IMAGES = {'python': 'python:3.10-slim', 'node': 'node:22-slim'}


def languages(paths):
    result = []
    if any(path.endswith('.py') for path in paths): result.append('python')
    if any(path.endswith('.js') for path in paths): result.append('node')
    return result


def inspect_image(language):
    override = os.environ.get('DOCKER_HOST', '')
    if override and not override.startswith(('unix:///', 'npipe:////./pipe/')):
        raise ValueError('用户代码检查仅使用本机Docker，拒绝将源码发送到远端Docker。')
    context = subprocess.run(['docker', 'context', 'inspect', '--format', '{{json .Endpoints.docker.Host}}'], capture_output=True, timeout=5)
    if context.returncode: raise ValueError('无法确认本机Docker连接。')
    endpoint = json.loads(context.stdout)
    if not isinstance(endpoint, str) or not endpoint.startswith(('unix:///', 'npipe:////./pipe/')):
        raise ValueError('当前Docker连接不是本机，未发送源码。')
    result = subprocess.run(['docker', 'image', 'inspect', IMAGES[language]], capture_output=True, timeout=8)
    if result.returncode: raise ValueError('所需隔离检查镜像尚未准备。')
    value = json.loads(result.stdout)[0]
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', value['Id']) or value.get('Os') != 'linux':
        raise ValueError('检查镜像身份或平台不符合要求。')
    if not any(item.startswith((language + '@sha256:', 'docker.io/library/' + language + '@sha256:')) for item in value.get('RepoDigests', [])):
        raise ValueError('镜像没有对应的官方来源摘要，未启动检查。')
    result = {'image_id': value['Id'], 'tag': IMAGES[language], 'repo_digests': value['RepoDigests']}
    if language == 'python':
        from source_pytest_bundle import pytest_bundle
        result['test_tools'] = pytest_bundle()[1]
    return result


def runtime_status():
    values, errors = {}, []
    for language in IMAGES:
        try: values[language] = inspect_image(language)
        except (OSError, ValueError, subprocess.SubprocessError): errors.append(language)
    return {'available': values, 'missing': errors,
            'notice': '隔离检查环境未就绪。请先打开Docker，再准备运行环境；不会修改或重启已有项目服务。' if errors else '隔离检查环境已就绪。',
            'limits': '当前支持Python 3.10的unittest/pytest及Node 22内置测试；项目额外依赖不会自动安装。'}


def require_runtime(paths):
    kinds = languages(paths)
    if not kinds: raise ValueError('当前隔离运行检查支持Python或JavaScript；其他项目可分析，修复运行验收尚不支持。')
    try: return {language: inspect_image(language) for language in kinds}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise ValueError('隔离检查环境尚未就绪，请先检查/准备运行环境。本次未创建修复任务，也未调用模型。') from error


def start_preparation(home):
    path = Path(home) / 'source-runtime-preparation.json'
    if path.exists():
        old = json.loads(path.read_bytes())
        if old.get('status') == 'preparing' and old.get('owner_pid') == os.getpid(): return old
    value = {'status': 'preparing', 'owner_pid': os.getpid(), 'notice': '正在准备公开检查镜像，不包含用户源码，不调用模型。'}
    path.write_bytes(canonical_bytes(value))
    def prepare():
        try:
            for image in IMAGES.values():
                result = subprocess.run(['docker', 'pull', image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
                if result.returncode: raise ValueError('镜像准备未完成。请确认Docker已经开启、网络可用。')
            value = {'status': 'ready', **runtime_status()}
        except (OSError, ValueError, subprocess.SubprocessError):
            value = {'status': 'failed', 'notice': '检查环境准备未完成。请先打开Docker并检查网络；未修改现有服务。'}
        temporary = path.with_suffix('.tmp'); temporary.write_bytes(canonical_bytes(value)); temporary.replace(path)
    threading.Thread(target=prepare, daemon=True).start()
    return value


def run_checks(files, acceptance, runtime, *, output, deadline, check):
    results = []
    for language, binding in runtime.items():
        check()
        current = inspect_image(language)
        if current != binding:
            raise ValueError('检查镜像已改变，停止使用另一个版本。')
        remaining = int(deadline - time.monotonic())
        if remaining <= 0: raise TimeoutError('运行检查达到任务剩余期限。')
        seconds = min(45, remaining)
        name = 'source-check-' + uuid.uuid4().hex
        driver_path = ROOT / 'scripts' / ('source_check_driver.py' if language == 'python' else 'source_check_driver.js')
        driver = driver_path.read_text(encoding='utf8')
        command = ['docker', 'run', '--interactive', '--rm', '--init', '--pull=never', '--name', name, '--label', 'mini-source-check=' + name,
            '--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--user=65534:65534', '--pids-limit=64', '--memory=256m', '--memory-swap=256m', '--cpus=1',
            '--tmpfs', '/work:rw,nosuid,nodev,size=96m,mode=1777', '--tmpfs', '/tmp:rw,nosuid,nodev,size=32m,mode=1777',
            '--workdir=/work', '--entrypoint=/usr/bin/timeout', binding['image_id'], '--kill-after=2s', str(seconds) + 's',
            'python' if language == 'python' else 'node', '-B' if language == 'python' else '-e']
        command += ['-c', driver] if language == 'python' else [driver]
        tools, receipt = {}, uuid.uuid4().hex
        if language == 'python':
            from source_pytest_bundle import pytest_bundle
            tools, tool_binding = pytest_bundle()
            if tool_binding != binding.get('test_tools'): raise ValueError('Python检查组件版本已改变，停止旧任务。')
        payload = {'files': {name: base64.b64encode(raw).decode() for name, raw in files.items()},
                   'tools': {name: base64.b64encode(raw).decode() for name, raw in tools.items()}, 'receipt': receipt,
                   'acceptance': {name: text for name, text in acceptance.items() if name.endswith('.py' if language == 'python' else '.js')}, 'timeout': seconds}
        log = Path(output) / (name + '.log')
        payload_file = Path(output) / (name + '-input.json')
        payload_file.write_bytes(canonical_bytes(payload))
        begin = time.monotonic()
        timed_out = False
        # No host mounts and no model-provided command arguments. Docker's own
        # timeout remains effective if the panel's worker is forcibly stopped.
        with log.open('wb') as stream, payload_file.open('rb') as input_stream:
            process = subprocess.Popen(command, stdin=input_stream, stdout=stream, stderr=stream)
            try:
                while process.poll() is None:
                    check()
                    if time.monotonic() >= deadline or time.monotonic() - begin >= seconds + 3 or log.stat().st_size > 1000000:
                        timed_out = True
                        break
                    time.sleep(0.05)
            finally:
                if process.poll() is None:
                    process.kill(); process.wait(timeout=5)
                    # Stop only the exact owned container. Never prune or enumerate
                    # and delete containers belonging to another task/project.
                    inspected = subprocess.run(['docker', 'inspect', name, '--format', '{{index .Config.Labels "mini-source-check"}}'], capture_output=True, timeout=5)
                    if inspected.returncode == 0 and inspected.stdout.decode().strip() == name:
                        subprocess.run(['docker', 'stop', '--time=1', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        check()
        raw = log.read_bytes()
        text = raw[-20000:].decode('utf8', errors='replace')
        matches = re.findall(r'^SOURCE_CHECK_RESULT_' + receipt + r'=(\{[^\r\n]+\})$', text, re.M)
        report = json.loads(matches[-1]) if len(matches) == 1 else {'tests': 0, 'errors': 1, 'failures': 0, 'skipped': 0, 'reason': 'No unique check-driver result'}
        successful = (not timed_out and process.returncode == 0 and report.get('tests', 0) > report.get('skipped', 0)
                      and report.get('errors') == 0 and report.get('failures') == 0 and not report.get('syntax_errors'))
        results.append({'language': language, 'image_id': binding['image_id'], 'exit_code': process.returncode,
                        'timed_out': timed_out, 'passed': successful, 'actual_execution': True, 'runner': 'docker_networkless',
                        'elapsed_seconds': round(time.monotonic() - begin, 3), 'report': report,
                        'log': log.name, 'log_sha256': sha256(raw), 'output_tail': text[-6000:]})
    return {'passed': bool(results) and all(item['passed'] for item in results), 'checks': results,
            'tree_sha256': sha256(canonical_bytes({path: sha256(raw) for path, raw in sorted(files.items())})),
            'note': '程序实际运行隔离检查并记录退出码；项目测试与模型生成的验收仍可能覆盖不足，不等于全仓没有问题。'}
