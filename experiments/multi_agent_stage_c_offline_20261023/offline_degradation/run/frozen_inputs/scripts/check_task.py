"""Independent evaluator using frozen external checks and protected upstream tests."""
import argparse
from datetime import datetime
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True
from task_workspace import ROOT, digest, load_task, package_root


class CountingResult(unittest.TextTestResult):
    subtests_run = 0

    def addSubTest(self, test, subtest, err):
        self.subtests_run += 1
        super().addSubTest(test, subtest, err)


def run_suite(suite) -> dict:
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=1, resultclass=CountingResult).run(suite)
    parent_id = lambda test: getattr(test, "test_case", test).id()
    failed_methods = {parent_id(test) for test, _ in result.failures + result.errors}
    return {"tests_run": result.testsRun, "subtests_run": result.subtests_run,
            "passed": result.testsRun - len(failed_methods) - len(result.skipped) - len(result.expectedFailures) - len(result.unexpectedSuccesses),
            "failed_test_methods": sorted(failed_methods),
            "failures": [test.id() for test, _ in result.failures], "errors": [test.id() for test, _ in result.errors],
            "skipped": [test.id() for test, _ in result.skipped], "successful": result.wasSuccessful(),
            "output": stream.getvalue()[-16000:]}


def pytest_suite(repo: Path, target: str | list[str], result_dir: Path, name: str, *, plugins=(), import_root=".") -> dict:
    xml = result_dir / (name + ".xml")
    temporary = repo / "temp" / ("pytest_" + datetime.now().strftime("%H%M%S_%f"))
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
               TEMP=str(repo / "temp"), TMP=str(repo / "temp"),
               PYTHONPATH=str(package_root(repo, {"import_root": import_root})))
    targets = [target] if isinstance(target, str) else target
    if not targets or not all(isinstance(item, str) and item for item in targets):
        raise ValueError("Expected nonempty upstream pytest targets.")
    plugin_args = [part for plugin in plugins for part in ("-p", plugin)]
    result = subprocess.run([sys.executable, "-B", "-m", "pytest", *targets, "-q", "-p", "no:cacheprovider",
                *plugin_args, "--basetemp", str(temporary), "--junitxml", str(xml)], cwd=repo, env=env,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    if not xml.is_file():
        raise RuntimeError("Upstream pytest did not produce a report.")
    cases = list(ET.parse(xml).getroot().iter("testcase"))
    ids = lambda kind: [case.get("classname", "") + "." + case.get("name", "")
                        for case in cases if case.find(kind) is not None]
    failures, errors, skipped = ids("failure"), ids("error"), ids("skipped")
    return {"tests_run": len(cases), "passed": len(cases) - len(failures) - len(errors) - len(skipped),
            "failures": failures, "errors": errors, "skipped": skipped,
            "successful": result.returncode == 0, "returncode": result.returncode,
            "output": (result.stdout + result.stderr)[-16000:]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--full-suite", action="store_true")
    args = parser.parse_args()
    task, manifest = load_task(args.task)
    repo, output = args.repo.resolve(), args.output.resolve()
    if not repo.is_relative_to(ROOT) or not any(output.is_relative_to(base) for base in (ROOT / "temp", task / "temp")):
        raise SystemExit("Use a project checkout and temp report output.")
    output.parent.mkdir(parents=True, exist_ok=True)
    (repo / "temp").mkdir(exist_ok=True)
    changed_tests = [relative for relative, expected in manifest["source_files_sha256"].items()
                     if relative.startswith("tests/") and (not (repo / relative).is_file() or digest(repo / relative) != expected)]
    sys.path.insert(0, str(package_root(repo, manifest)))
    package = __import__(manifest["package"])
    if not Path(package.__file__).resolve().is_relative_to(repo):
        raise RuntimeError("Evaluator imported the wrong package.")
    loader = unittest.TestLoader()
    spec = importlib.util.spec_from_file_location("frozen_behavior_checks", task / "checks/test_behavior.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    issue = run_suite(loader.loadTestsFromModule(module))
    if manifest["upstream_runner"] == "pytest":
        upstream = pytest_suite(repo, manifest["upstream_target"], output.parent, "upstream", plugins=manifest.get("pytest_plugins", ()), import_root=manifest.get("import_root", "."))
    else:
        upstream = run_suite(loader.loadTestsFromName(manifest["upstream_target"]))
    report = {"task_id": manifest["task_id"], "repo": str(repo), "imported_package": package.__file__,
              "issue_checks": issue, "upstream_targeted": upstream,
              "protected_tests_unchanged": not changed_tests, "changed_protected_tests": changed_tests,
              "task_tests_passed": issue["successful"] and upstream["successful"] and not changed_tests,
              "scope": manifest["scope"], "external_model_requests": 0}
    if args.full_suite:
        if manifest["upstream_runner"] == "pytest":
            full = pytest_suite(repo, manifest["full_target"], output.parent, "full", plugins=manifest.get("pytest_plugins", ()), import_root=manifest.get("import_root", "."))
        else:
            full = run_suite(loader.discover(str(repo / manifest["full_target"])))
        report["full_upstream_suite"] = full
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: ({k: v for k, v in value.items() if k != "output"} if isinstance(value, dict) else value)
                      for key, value in report.items()}, ensure_ascii=False, indent=2))
    return 0 if report["task_tests_passed"] and report.get("full_upstream_suite", {}).get("successful", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
