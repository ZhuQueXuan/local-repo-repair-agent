"""Separate upstream compatibility checks from project extension regression."""
import argparse
import ast
import asyncio
from datetime import datetime
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parent.parent
UPSTREAM = ROOT / "mini-swe-agent"
CONFIG = ROOT / "configs/upstream_regression.json"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fingerprint(paths):
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None for path in sorted(paths)}


def upstream_child(output, profile):
    from upstream_regression_support import UpstreamResults, install_guards
    configuration = json.loads(CONFIG.read_text(encoding="utf-8"))
    scratch = output / "scratch"
    scratch.mkdir()
    # Windows' event-loop self-pipe is local IPC. Initialize it before the
    # network guard; no server/model request is involved, and close it below.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    violations = install_guards(scratch)
    sys.path[:0] = [str(UPSTREAM), str(UPSTREAM / "src")]
    os.chdir(UPSTREAM)
    import pytest
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.output import DummyOutput
    plugin = UpstreamResults(configuration, output / "upstream_results.json", profile)
    files = sorted(path.relative_to(UPSTREAM).as_posix() for path in (UPSTREAM / "tests").rglob("test_*.py"))
    arguments = configuration["native_files"] + configuration["adapter_files"]
    if profile == "native":
        arguments = configuration["native_files"]
    elif profile == "adapter":
        arguments = configuration["adapter_files"]
    # Upstream interactive modules instantiate a terminal during import. Supply
    # prompt_toolkit's own headless session; model/agent assertions are unchanged.
    with create_app_session(input=DummyInput(), output=DummyOutput()):
        code = pytest.main([*arguments, "-v", "--tb=short", "-p", "no:cacheprovider", "-p", "pytest_mock",
                            "--basetemp", str(scratch / "pytest"), "-o", "tmp_path_retention_policy=all",
                            "--junitxml", str(output / "upstream_junit.xml")], plugins=[plugin])
    loop.close()
    asyncio.set_event_loop(None)
    plugin.save(code, violations)
    return int(code) or (1 if violations else 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("all", "upstream", "project"), default="all")
    parser.add_argument("--upstream-profile", choices=("both", "native", "adapter", "audit"), default="native")
    parser.add_argument("--upstream-child", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.upstream_child:
        return upstream_child(args.upstream_child, args.upstream_profile)
    output = ROOT / "temp/regression" / (datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8])
    output.mkdir(parents=True)
    configuration = json.loads(CONFIG.read_text(encoding="utf-8"))
    init_tree = ast.parse((UPSTREAM / "src/minisweagent/__init__.py").read_text(encoding="utf-8"))
    version = next(ast.literal_eval(n.value) for n in init_tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in n.targets))
    if version != configuration["upstream_version"]:
        raise SystemExit("Upstream version changed; review the offline test profile first.")
    tests = sorted((UPSTREAM / "tests").rglob("test_*.py"))
    classified = set(configuration["native_files"]) | set(configuration["adapter_files"]) | set(configuration["excluded_files"])
    discovered = {path.relative_to(UPSTREAM).as_posix() for path in tests}
    if classified != discovered:
        raise SystemExit("Upstream test inventory changed; review configuration before running. New: " + str(sorted(discovered - classified)) + "; missing: " + str(sorted(classified - discovered)))
    inventory = []
    for path in tests:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        inventory.append({"file": path.relative_to(UPSTREAM).as_posix(),
                          "test_functions": sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_") for n in ast.walk(tree)),
                          "profile": "native" if path.relative_to(UPSTREAM).as_posix() in configuration["native_files"] else "adapter" if path.relative_to(UPSTREAM).as_posix() in configuration["adapter_files"] else "excluded",
                          "file_exclusion": configuration["excluded_files"].get(path.relative_to(UPSTREAM).as_posix())})
    write_json(output / "inventory.json", inventory)
    tracked = [p for folder in ("src", "tests") for p in (UPSTREAM / folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    tracked.extend(p for folder in ("scripts", "configs", "data") for p in (ROOT / folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    # The running panel holds this transient IPC lock exclusively on Windows.
    # It contains no persisted task data; protect every other data file.
    transient_locks = {ROOT / "data/task_service/server.lock"}
    tracked = [p for p in tracked if p not in transient_locks]
    before = fingerprint(tracked)
    write_json(output / "protected_before.json", before)
    # Preserve OS/runtime variables, dropping provider credentials and user config.
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "SYSTEMDRIVE", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                        "MSWEA_GLOBAL_CONFIG_DIR": str(output / "global_config"), "MSWEA_SILENT_STARTUP": "1",
                        "LITELLM_LOCAL_MODEL_COST_MAP": "True", "PYTHON_DOTENV_DISABLED": "1", "HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
                        "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1",
                        "TEMP": str(output / "os_temp"), "TMP": str(output / "os_temp"),
                        "MINI_TASK_BASH": os.environ.get("MINI_TASK_BASH", "D:\\Git\\bin\\bash.exe"),
                        "PYTHONPATH": os.pathsep.join((str(UPSTREAM / "src"), str(UPSTREAM), str(ROOT / "scripts")))})
    (output / "os_temp").mkdir()
    # The guard's permitted output root is output (scratch.parent).
    jobs = []
    if args.scope in {"all", "upstream"}:
        profiles = ("native", "adapter") if args.upstream_profile == "both" else (args.upstream_profile,)
        for profile in profiles:
            child_output = output / profile
            child_output.mkdir()
            jobs.append((profile, [sys.executable, "-B", str(Path(__file__).resolve()), "--upstream-child", str(child_output), "--upstream-profile", profile]))
    if args.scope in {"all", "project"}:
        jobs.append(("project", [sys.executable, "-B", "-m", "unittest", "discover", "-s", "scripts", "-p", "test_*.py", "-v"]))
    outcomes = []
    for label, command in jobs:
        print("Running " + label + "; log: " + str(output / (label + ".log")), flush=True)
        job_env = environment | {"MSWEA_GLOBAL_CONFIG_DIR": str(output / label / "global_config"),
                                  "TEMP": str(output / label / "os_temp"), "TMP": str(output / label / "os_temp")}
        (output / label / "os_temp").mkdir(parents=True)
        with (output / (label + ".log")).open("wb") as log:
            result = subprocess.run(command, cwd=ROOT, env=job_env, stdout=log, stderr=subprocess.STDOUT)
        outcome = {"scope": label, "exit_code": result.returncode, "command": command}
        if label != "project" and (output / label / "upstream_results.json").is_file():
            details = json.loads((output / label / "upstream_results.json").read_text(encoding="utf-8"))
            outcome.update({"counts": details["counts"], "deselected": len(details["deselected"]), "guard_violations": len(details["guard_violations"])})
        elif label == "project":
            log_text = (output / "project.log").read_text(encoding="utf-8", errors="replace")
            count = re.search(r"Ran (\d+) tests? in", log_text)
            skipped = re.search(r"skipped=(\d+)", log_text)
            outcome.update({"tests_run": int(count[1]) if count else None, "skipped": int(skipped[1]) if skipped else 0})
        outcomes.append(outcome)
        print(json.dumps(outcome, ensure_ascii=False), flush=True)
    after = fingerprint(tracked)
    write_json(output / "protected_after.json", after)
    changed = [name for name in before if before[name] != after[name]]
    summary = {"scope": args.scope, "upstream_version": version, "jobs": outcomes, "protected_files": len(before), "changed_protected_files": changed,
               "transient_lock_exclusions": [p.relative_to(ROOT).as_posix() for p in sorted(transient_locks)],
               "upstream_test_files": len(tests), "upstream_test_functions": sum(row["test_functions"] for row in inventory),
               "configuration": configuration, "versions": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}}
    write_json(output / "summary.json", summary)
    print("Summary: " + str(output / "summary.json"), flush=True)
    return 1 if changed or any(row["exit_code"] for row in outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())
