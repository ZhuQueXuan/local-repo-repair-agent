"""Disclosed manual candidate and independent checks for one feedback diagnostic.

This is wiring evidence on a development task, never an autonomous repair score.
"""
import argparse
import json
from pathlib import Path
import sys

from collaboration_protocol import ProtocolError
from task_workspace import ROOT, digest

PROFILE = "seeded_falsey_remainder_v1"
TASK = "split-after-boundary"
SOURCE = "more_itertools/more.py"
OLD = "            if maxsplit == 1:\n                yield list(it)\n"
SEEDED = "            if maxsplit == 1:\n                rest = list(it)\n                if rest and any(rest):\n                    yield rest\n"
FIXED = "            if maxsplit == 1:\n                rest = list(it)\n                if rest:\n                    yield rest\n"
SCOPE = "Manual known-defect candidate, disclosed falsey-remainder checks; real-model feedback wiring only, not discovery or effectiveness evidence."


def validate_policy(policy, task, profile, execution):
    selected = policy.get("diagnostic_profile")
    if selected is not None and (selected != PROFILE or task != TASK or profile != "adaptive_history" or execution != "live"):
        raise ValueError("Diagnostic requires the fixed disclosed task, profile and live execution")
    return selected == PROFILE


def seed_candidate(checkout):
    path = checkout / SOURCE
    raw = path.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8").replace("\r\n", "\n")
    start = text.index("\ndef split_after(")
    end = text.index("\ndef split_when(", start)
    body = text[start:end]
    if body.count(OLD) != 1:
        raise ProtocolError("diagnostic_seed_source_mismatch")
    path.write_text(text[:start] + body.replace(OLD, SEEDED) + text[end:], encoding="utf-8", newline=newline)
    return {"origin": "manual_seed", "profile": PROFILE, "scope": SCOPE,
            "path": SOURCE, "seed_sha256": digest(path)}


def assert_confirmation_unchanged(checkout, manifest, metadata):
    for relative, expected in manifest["source_files_sha256"].items():
        target = checkout / relative
        if not target.is_file() or target.is_symlink() or target.stat().st_nlink != 1:
            raise ProtocolError("diagnostic_confirmation_changed_source")
        expected = metadata["seed_sha256"] if relative == SOURCE else expected
        if digest(target) != expected:
            raise ProtocolError("diagnostic_confirmation_changed_source")


def check_cases(split_after):
    cases = (("zero", [1, 0], [[1], [0]]), ("none", [1, None], [[1], [None]]),
             ("false", [1, False], [[1], [False]]), ("zeros", [1, 0, 0], [[1], [0, 0]]),
             ("nonempty", [1, 2, 3], [[1], [2, 3]]), ("empty_tail", [1], [[1]]))
    results = []
    for name, values, expected in cases:
        actual = list(split_after(iter(values), lambda value: value == 1, maxsplit=1))
        # JSON also distinguishes False from 0 in this declared fixture.
        results.append({"id": name, "expected": expected, "actual": actual,
                        "passed": json.dumps(actual) == json.dumps(expected)})
    passed = sum(r["passed"] for r in results)
    return {"schema_version": 1, "profile": PROFILE, "scope": SCOPE,
            "tests_run": len(results), "passed": passed, "successful": passed == len(results),
            "failed_cases": [r["id"] for r in results if not r["passed"]], "cases": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    repo, output = args.repo.resolve(), args.output.resolve()
    task_temp = ROOT / "tasks" / TASK / "temp"
    if not repo.is_relative_to(task_temp) or not output.is_relative_to(ROOT / "temp"):
        raise SystemExit("Use the fixed task checkout and project temp report output")
    sys.path.insert(0, str(repo))
    import more_itertools
    if not Path(more_itertools.__file__).resolve().is_relative_to(repo):
        raise RuntimeError("Diagnostic imported outside the checkout")
    report = check_cases(more_itertools.split_after)
    report["imported_package"] = more_itertools.__file__
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}))
    return 0 if report["successful"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
