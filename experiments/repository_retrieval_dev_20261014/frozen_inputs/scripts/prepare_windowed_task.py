"""Create a fresh task checkout under temp without changing the frozen base."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
TASK = ROOT / "tasks" / "windowed-empty"
sys.dont_write_bytecode = True


def main():
    manifest = json.loads((TASK / "manifest.json").read_text(encoding="utf-8"))
    source = TASK / "repo"
    for name, digest in manifest["source_files_sha256"].items():
        path = source / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise SystemExit("Frozen source changed: " + name)
    batch = TASK / "temp" / ("run_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    work = batch / "repo"
    work.mkdir(parents=True)
    for name in manifest["source_files_sha256"]:
        destination = work / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, destination)
    (work / "temp").mkdir()
    (work / "task_checks").mkdir()
    shutil.copy2(TASK / "checks" / "test_issue_462.py", work / "task_checks" / "test_issue_462.py")
    shutil.copy2(TASK / "issue.md", work / "TASK.md")
    record = {"task_id": manifest["task_id"], "base_commit": manifest["base_commit"],
              "work": str(work), "external_model_requests": 0,
              "upstream_tests_command": "python -B -m unittest tests.test_more.WindowedTests -v",
              "issue_tests_command": "python -B -m unittest discover -s task_checks -v"}
    (batch / "setup.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
