"""Explicit host runtime declarations and bounded, user-triggered version probes."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import sysconfig

from execution_backend import LocalProcessBackend

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "configs/execution_backends_v1.json"


def load_execution_config():
    raw = CONFIG.read_bytes()
    if len(raw) > 16384:raise ValueError("Execution configuration too large")
    config = json.loads(raw)
    if set(config) != {"schema_version", "backend", "bash", "python", "node", "git", "version_probe_timeout_seconds", "automatic_install", "os_sandbox", "dependency_manifests"}:
        raise ValueError("Unknown execution configuration fields")
    if type(config["schema_version"]) is not int or config["schema_version"] != 1 or config["backend"] != "windows_git_bash_v1":
        raise ValueError("Unsupported execution backend")
    if config["automatic_install"] is not False or config["os_sandbox"] is not False:
        raise ValueError("This backend neither installs packages nor provides OS isolation")
    if config["bash"] != {"executable_env": "MINI_TASK_BASH", "fallback": r"D:\Git\bin\bash.exe"} or config["python"] != {
            "executable_source": "current_process", "minimum_version": [3, 10], "dependency_roots": "current_process_prefix"}:
        raise ValueError("Unsupported shell or Python selection policy")
    if config["node"] != {"executable_env": "MINI_TASK_NODE", "fallback_command": "node", "required_for_python_tasks": False, "automatic_task_service_support": False} or config["git"] != {"command": "git"}:
        raise ValueError("Unsupported Node or Git policy")
    if type(config["version_probe_timeout_seconds"]) is not int or not 1 <= config["version_probe_timeout_seconds"] <= 5:
        raise ValueError("Invalid bounded version probe timeout")
    expected = ["configs/core-offline-requirements.txt", "configs/model-requirements.txt", "configs/evaluation-requirements.txt", "configs/retrieval-evaluation-requirements.txt"]
    if config["dependency_manifests"] != expected:raise ValueError("Unknown dependency manifest paths")
    return config


def bash_executable():
    config = load_execution_config()
    value = os.environ.get(config["bash"]["executable_env"], config["bash"]["fallback"])
    if not value or any(c in value for c in ('\0', '\r', '\n')):
        raise ValueError("Invalid MINI_TASK_BASH executable path")
    return Path(_resolve(value) or value)


def _resolve(value):
    if not value or any(c in value for c in ('\0', '\r', '\n')):return None
    path = Path(value)
    resolved = str(path.absolute()) if path.is_file() else shutil.which(value)
    return str(Path(resolved).absolute()) if resolved else None


def _probe(path, args, timeout):
    if path is None:return {"status": "missing", "version": None}
    env = {k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}}
    env.update(TEMP=str(ROOT/'temp'), TMP=str(ROOT/'temp'), HOME=str(ROOT/'temp/bash-home'))
    try:
        result = LocalProcessBackend().run_argv([path, *args], cwd=ROOT/'temp', env=env, timeout=timeout, text=True)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"status": "unavailable", "version": None, "reason": type(error).__name__}
    line = (result.output.strip().splitlines() or [''])[0]
    valid = result.returncode == 0 and 0 < len(line) <= 240 and bool(re.search(r'\d+\.\d+', line))
    return {"status": "available" if valid else "probe_failed", "version": line if valid else None,
            "returncode": result.returncode, "elapsed_seconds": result.elapsed_seconds}


def runtime_report(*, probe=False):
    config = load_execution_config()
    bash = _resolve(str(bash_executable()))
    node_value = os.environ.get(config['node']['executable_env'], config['node']['fallback_command'])
    node, git = _resolve(node_value), _resolve(config['git']['command'])
    timeout = config['version_probe_timeout_seconds']
    python = {"executable": sys.executable, "version": '.'.join(map(str, sys.version_info[:3])),
        "status": "available" if sys.version_info >= (3,10) else "unsupported_version", "source": "current_process",
        "prefix": sys.prefix, "base_prefix": sys.base_prefix, "site_packages": sysconfig.get_path('purelib')}
    runtimes = {"python": python}
    for name, path, args in [('bash', bash, ['--noprofile','--norc','--version']), ('node', node, ['--version']), ('git', git, ['--version'])]:
        runtimes[name] = {"executable": path, **(_probe(path,args,timeout) if probe else {"status": "not_probed" if path else "missing", "version": None})}
    runtimes['node'].update(required_for_python_tasks=False,automatic_task_service_support=False,
        selection='MINI_TASK_NODE' if 'MINI_TASK_NODE' in os.environ else 'PATH')
    packages = {}
    for name in ('mini-swe-agent','pydantic','litellm','pytest','python-dotenv'):
        try:packages[name] = {'version':importlib.metadata.version(name),'status':'installed_metadata'}
        except importlib.metadata.PackageNotFoundError:packages[name] = {'version':None,'status':'missing'}
    manifests = [{"path":name,"sha256":hashlib.sha256((ROOT/name).read_bytes()).hexdigest()} for name in config['dependency_manifests']]
    return {"schema_version":1,"recorded_at":datetime.now(timezone.utc).isoformat(),"probe_performed":probe,"automatic_task_service_languages":["python"],
        "backend":LocalProcessBackend().describe(),"configuration_sha256":hashlib.sha256(CONFIG.read_bytes()).hexdigest(),
        "runtimes":runtimes,"dependencies":{"python_packages":packages,"manifests":manifests,
            "node_modules_installed_by_this_check":False,"node_scope":"Runtime declaration only; no npm/package installation or generic JS task-service adapter."},
        "scope":"No model calls, installs or network probes. A version probe does not certify task execution, dependency compatibility or OS isolation."}


def runtime_identity():
    """Bound currently declared Python-task environment, excluding optional Node."""
    report = runtime_report()
    value = {"configuration_sha256": report['configuration_sha256'], "platform": sys.platform,
        "python": {key:report['runtimes']['python'][key] for key in ('executable','version','prefix','base_prefix','site_packages')},
        "bash_executable": report['runtimes']['bash']['executable'], "git_executable": report['runtimes']['git']['executable'],
        "python_dependency_metadata": report['dependencies']['python_packages'], "dependency_manifests": report['dependencies']['manifests']}
    raw = json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    return {"sha256":hashlib.sha256(raw).hexdigest(),"declaration":value,
            "scope":"Declared paths, Python version and selected package metadata; not a full binary/dependency attestation or cross-machine migration."}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe',action='store_true',help='Actually execute the fixed Bash/Node/Git version commands')
    args=parser.parse_args()
    print(json.dumps(runtime_report(probe=args.probe),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
