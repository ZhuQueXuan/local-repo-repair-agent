"""Sequential declared batch; no retries or model-result-driven changes."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from evaluation_freeze import verify_freeze

parser = argparse.ArgumentParser()
parser.add_argument("--run", action="store_true")
args = parser.parse_args()
name = "retrieval_delivery_trial_20261015"
config = json.loads((ROOT / "configs" / (name + ".json")).read_text(encoding="utf-8"))
order = config["run_order"] if args.run else [{"task": task, "variant": "retrieval_on_change"} for task in config["tasks"]]
progress = ROOT / "temp" / (name + ("_batch_status.json" if args.run else "_preflight_status.json"))
assert not progress.exists(), "Never repeat an existing batch"
rows = []
for item in order:
    verify_freeze(ROOT, config["freeze_manifest"])
    console = ROOT / "temp" / (name + "_" + item["task"] + "_" + item["variant"] + ("_run.log" if args.run else "_preflight.log"))
    row = item | {"status": "running", "console": str(console)}
    rows.append(row)
    progress.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    command = [sys.executable, "-B", str(ROOT / "scripts/run_task_batch.py"), "--task", item["task"], "--config", name + ".json", "--variant", item["variant"]]
    if args.run:
        command.append("--run")
    with console.open("w", encoding="utf-8") as stream:
        result = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, timeout=600)
    row.update(status="finished" if result.returncode == 0 else "collector_or_preflight_error", returncode=result.returncode)
    if args.run:
        claim = ROOT / "temp" / (name + "_" + item["task"] + "_" + item["variant"] + "_claim.json")
        if claim.exists():
            row["results"] = json.loads(claim.read_text(encoding="utf-8"))["results"]
            summary = Path(row["results"]) / "summary.json"
            if summary.exists():
                report = json.loads(summary.read_text(encoding="utf-8"))
                row.update(exit_status=report["exit_status"], repair_verified=report["repair_verified"], requests=report["requests"])
    progress.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(json.dumps(row, indent=2), flush=True)
    if result.returncode != 0:
        raise SystemExit("Stop on collection/preflight error; preserve evidence and recover offline before proceeding.")
