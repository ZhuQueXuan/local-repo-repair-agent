"""Check a supplied checkout using frozen issue checks and upstream tests."""
import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import platform
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
TASK = ROOT / "tasks" / "windowed-empty"
sys.dont_write_bytecode = True


def run_suite(suite):
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=1).run(suite)
    return {
        "tests_run": result.testsRun,
        "passed": result.testsRun - len(result.failures) - len(result.errors)
                  - len(result.skipped) - len(result.expectedFailures) - len(result.unexpectedSuccesses),
        "failures": [test.id() for test, _ in result.failures],
        "errors": [test.id() for test, _ in result.errors],
        "skipped": [test.id() for test, _ in result.skipped],
        "expected_failures": [test.id() for test, _ in result.expectedFailures],
        "unexpected_successes": [test.id() for test in result.unexpectedSuccesses],
        "successful": result.wasSuccessful(),
        "output": stream.getvalue()[:16000],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=TASK / "repo")
    parser.add_argument("--full-suite", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve()
    output = (args.output or ROOT / "temp" / ("windowed_check_" + time.strftime("%Y%m%d_%H%M%S") + ".json")).resolve()
    if not any(output.is_relative_to(base.resolve()) for base in (ROOT / "temp", TASK / "temp")):
        raise SystemExit("Write intermediate reports to project temp/ or task temp/.")
    if not (repo / "more_itertools" / "__init__.py").is_file():
        raise SystemExit("A more-itertools checkout is required.")
    manifest = json.loads((TASK / "manifest.json").read_text(encoding="utf-8"))
    changed_tests = [
        name for name, digest in manifest["source_files_sha256"].items()
        if name.startswith("tests/") and (
            not (repo / name).is_file() or hashlib.sha256((repo / name).read_bytes()).hexdigest() != digest
        )
    ]
    sys.path.insert(0, str(repo))
    import more_itertools
    if not Path(more_itertools.__file__).resolve().is_relative_to(repo):
        raise SystemExit("Wrong installed package imported; stop evaluation.")

    loader = unittest.TestLoader()
    upstream = run_suite(loader.loadTestsFromName("tests.test_more.WindowedTests"))
    spec = importlib.util.spec_from_file_location("issue_462_checks", TASK / "checks" / "test_issue_462.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    issue = run_suite(loader.loadTestsFromModule(module))
    report = {
        "task_id": "more-itertools-windowed-empty-462",
        "repo": str(repo), "imported_package": more_itertools.__file__,
        "package_version": more_itertools.__version__, "python": platform.python_version(),
        "platform": platform.system(), "external_model_requests": 0,
        "upstream_windowed": upstream, "issue_checks": issue,
        "protected_tests_unchanged": not changed_tests,
        "changed_protected_tests": changed_tests,
        "task_tests_passed": upstream["successful"] and issue["successful"] and not changed_tests,
        "scope": "Selected Windows local task; not SWE-bench or an Agent performance baseline.",
    }
    if args.full_suite:
        report["full_upstream_suite"] = run_suite(loader.discover(str(repo / "tests")))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {key: value for key, value in report.items() if key not in ("upstream_windowed", "issue_checks", "full_upstream_suite")}
    for name in ("upstream_windowed", "issue_checks", "full_upstream_suite"):
        if name in report:
            summary[name] = {key: value for key, value in report[name].items() if key != "output"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("Report: " + str(output))
    # A full-suite failure also matters even if the selected issue has been fixed.
    full_passed = report.get("full_upstream_suite", {}).get("successful", True)
    return 0 if report["task_tests_passed"] and full_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
