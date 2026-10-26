import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
os.environ["MSWEA_GLOBAL_CONFIG_DIR"] = str(ROOT / "temp" / "mini-global")
os.environ["MSWEA_SILENT_STARTUP"] = "1"
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "mini-swe-agent" / "src"))

from minisweagent import __version__
from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.test_models import DeterministicModel, make_output

WORK = ROOT / "examples" / "cart-empty" / "temp" / "offline_smoke_20261005"
RESULT = ROOT / "experiments" / "offline_smoke_20261005"
WORK.mkdir(parents=True, exist_ok=True)
RESULT.mkdir(parents=True, exist_ok=True)
for name in ("cart.py", "test_cart.py"):
    shutil.copy2(ROOT / "examples" / "cart-empty" / name, WORK / name)

def py_command(code):
    return subprocess.list2cmdline([sys.executable, "-B", "-c", code])

test_command = subprocess.list2cmdline([sys.executable, "-B", "-m", "unittest", "-v"])
baseline = subprocess.run(
    [sys.executable, "-B", "-m", "unittest", "-v"], cwd=WORK,
    text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=30,
)
baseline_text = baseline.stdout + baseline.stderr
(ROOT / "temp" / "cart_baseline.log").write_text(baseline_text, encoding="utf-8")
assert baseline.returncode != 0 and "TypeError" in baseline_text
assert "Ran 5 tests" in baseline_text and "FAILED (errors=1)" in baseline_text

steps = [
    ("Read the implementation.", py_command("from pathlib import Path; print(Path('cart.py').read_text())")),
    ("Reproduce the failure.", test_command),
    ("Apply the pre-scripted repair (not an autonomous model decision).",
     py_command("from pathlib import Path; p=Path('cart.py'); p.write_text(p.read_text().replace('reduce(add, prices)', 'reduce(add, prices, 0)'))")),
    ("Check the repair.", test_command),
    ("Submit the result.", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
]
model = DeterministicModel(
    outputs=[make_output(text, [{"command": command}], cost=0.0) for text, command in steps],
    cost_per_call=0.0,
)
agent = DefaultAgent(
    model, LocalEnvironment(cwd=str(WORK), timeout=30),
    system_template="Offline smoke test using pre-scripted actions.",
    instance_template="{{task}}", step_limit=6, cost_limit=0.01,
    wall_time_limit_seconds=120, output_path=RESULT / "trajectory.json",
)
result = agent.run("Fix empty cart totals and retain behavior for non-empty carts.")
assert result["exit_status"] == "Submitted"
assert agent.n_calls == 5 and agent.cost == 0.0
observations = [m["content"] for m in agent.messages if m.get("role") == "user"][1:]
assert len(observations) == 4
assert "FAILED (errors=1)" in observations[1]
assert "Ran 5 tests" in observations[3] and "OK" in observations[3]
assert model.current_index == 4
assert json.loads((RESULT / "trajectory.json").read_text())["info"]["exit_status"] == "Submitted"

limited = DefaultAgent(
    DeterministicModel(outputs=[make_output("Inspect", [{"command": "echo budget-probe"}], cost=0.0)] * 3, cost_per_call=0.0),
    LocalEnvironment(cwd=str(WORK), timeout=30),
    system_template="Offline budget check.", instance_template="{{task}}",
    step_limit=2, cost_limit=0.01, wall_time_limit_seconds=120,
    output_path=RESULT / "step_limit_trajectory.json",
)
limit_result = limited.run("Check the configured two-step limit.")
assert limit_result["exit_status"] == "LimitsExceeded" and limited.n_calls == 2

commit = subprocess.run(
    ["git", "rev-parse", "HEAD"], cwd=ROOT / "mini-swe-agent",
    text=True, capture_output=True, check=True,
).stdout.strip()
summary = {
    "kind": "offline_control_flow_smoke", "version": __version__, "commit": commit,
    "python": sys.version, "platform": sys.platform,
    "model": "upstream DeterministicModel; pre-scripted outputs",
    "external_model_requests": 0, "external_api_cost": 0,
    "environment": "upstream LocalEnvironment; Windows native shell (cmd.exe)",
    "baseline_tests": {"total": 5, "passed": 4, "errors": 1},
    "scripted_repair_tests": {"total": 5, "passed": 5},
    "repair_exit_status": result["exit_status"], "scripted_model_calls": agent.n_calls,
    "step_limit_exit_status": limit_result["exit_status"], "step_limit_calls": limited.n_calls,
    "checks": {"observations_in_history": True, "trajectory_saved": True},
    "limitations": ["No autonomous language model repair tested", "No Bash command compatibility certified", "No SWE-bench task or scoring run", "Full CLI/model dependencies not installed"],
}
(RESULT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
(RESULT / "repair.diff").write_text("--- a/cart.py\n+++ b/cart.py\n@@ -3,4 +3,4 @@\n \n \n def cart_total(prices):\n-    return reduce(add, prices)\n+    return reduce(add, prices, 0)\n", encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))
