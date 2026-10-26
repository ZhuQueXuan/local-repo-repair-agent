"""One teaching task with upstream DefaultAgent and explicit Windows/API adapters."""
import argparse
from decimal import Decimal
import difflib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
os.environ["MSWEA_GLOBAL_CONFIG_DIR"] = str(ROOT / "temp" / "mini-global")
os.environ["MSWEA_SILENT_STARTUP"] = "1"
os.environ["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] = "1"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
sys.dont_write_bytecode = True
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

from dotenv import dotenv_values
from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import InterruptAgentFlow, LimitsExceeded
from minisweagent.models.litellm_model import LitellmModel
from deepseek_budget import BudgetExhausted, SharedBudget, load_budget_config
from provider_priority import load_providers, classify_failure


class GitBashEnvironment(LocalEnvironment):
    def __init__(self, cwd, bash, *, python_import_root=None):
        super().__init__(cwd=str(cwd), timeout=30)
        self.bash = str(bash)
        self.python_import_root = None
        if python_import_root is not None:
            candidate = Path(python_import_root).resolve()
            if not candidate.is_relative_to(Path(cwd).resolve()) or not candidate.is_dir():
                raise ValueError("Python import root must be inside this checkout.")
            self.python_import_root = str(candidate)

    def execute(self, action, cwd="", *, timeout=None):
        if re.search(r"(?:^|[\s;&|])(?:rm|rmdir|del|rd|remove-item)(?:\s|$)", action["command"], re.IGNORECASE):
            return {"output": "Deletion commands are disabled for this task.", "returncode": -1, "exception_info": "Deletion rejected."}
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"}
        child_env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
        child_env.update({
            "PATH": str(Path(sys.executable).parent) + os.pathsep + child_env.get("PATH", ""),
            "HOME": str(ROOT / "temp" / "bash-home"), "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1", "TEMP": str(ROOT / "temp"),
            "TMP": str(ROOT / "temp"),
        })
        if self.python_import_root is not None:
            child_env["PYTHONPATH"] = self.python_import_root
        process = subprocess.Popen(
            [self.bash, "--noprofile", "--norc", "-c", action["command"]],
            cwd=cwd or self.config.cwd, env=child_env, text=True,
            encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        try:
            stdout, _ = process.communicate(timeout=timeout or self.config.timeout)
            output = {"output": stdout, "returncode": process.returncode, "exception_info": ""}
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            process.kill() if process.poll() is None else None
            stdout, _ = process.communicate(timeout=5)
            output = {"output": stdout, "returncode": -1, "exception_info": "Command timed out."}
        output = self._process_output(output, command=action["command"])
        self._check_finished(output)
        return output

    def _process_output(self, output, *, command):
        return output

    def serialize(self):
        data = super().serialize()
        data["info"]["config"]["bash_executable"] = self.bash
        if self.python_import_root is not None:
            data["info"]["config"]["python_import_root"] = self.python_import_root
        return data


class BudgetedDeepSeekModel(LitellmModel):
    """Retain upstream parsing/history; record shared usage and optional limits."""
    def __init__(self, settings, *, ledger_path=None, error_path=None, budget=None):
        self.budget_config = load_budget_config()
        maximum_call_cost = (Decimal(self.budget_config["max_prompt_tokens"]) * Decimal(self.budget_config["conservative_input_cny_per_million"])
                             + Decimal(self.budget_config["max_output_tokens"]) * Decimal(self.budget_config["conservative_output_cny_per_million"])) / Decimal(1_000_000)
        if (maximum_call_cost > Decimal(self.budget_config["reservation_per_attempt_cny"])
                or self.budget_config["max_messages_utf8_bytes"] + 8192 > self.budget_config["max_prompt_tokens"]):
            raise ValueError("Request token bounds exceed the monetary reservation.")
        self.budget = budget if budget is not None else SharedBudget(self.budget_config)
        self._reservation = None
        self.providers = load_providers(settings)
        if any(not provider.official_pricing for provider in self.providers) and self.budget.limit is not None:
            raise ValueError("Unknown gateway pricing requires the authorized uncapped usage policy.")
        self.provider_index = 0
        initial = self.providers[0]
        super().__init__(
            model_name="openai/" + initial.model,
            model_kwargs={"api_base": initial.api_base,
                          "max_tokens": self.budget_config["max_output_tokens"], "temperature": 0,
                          "timeout": 45, "num_retries": 0,
                          **({"extra_body": {"thinking": {"type": "disabled"}}}
                             if initial.supports_thinking_control else {})},
        )
        self._api_key = initial.api_key
        self._response_provider = None
        self.unpriced_requests = 0
        self.requests = 0
        self.actual_cost_cny = 0.0
        self.usage = []
        self.ledger_path = Path(ledger_path) if ledger_path else ROOT / "temp" / "deepseek_first_task_ledger.json"
        self.error_path = Path(error_path) if error_path else ROOT / "temp" / "deepseek_request_error.json"
        self.ledger = json.loads(self.ledger_path.read_text()) if self.ledger_path.exists() else {"attempts": 0}

    def contains_credentials(self, text):
        return any(provider.api_key in text for provider in self.providers)

    def _activate_provider(self, index):
        self.provider_index = index
        provider = self.providers[index]
        self.config.model_name = "openai/" + provider.model
        self.config.model_kwargs["api_base"] = provider.api_base
        if provider.supports_thinking_control:
            self.config.model_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        else:
            self.config.model_kwargs.pop("extra_body", None)
        self._api_key = provider.api_key
        return provider

    def provider_statistics(self):
        return {"priority": [provider.public() for provider in self.providers],
                "active_provider": self.providers[self.provider_index].name,
                "switches": self.ledger.get("provider_switches", []),
                "unpriced_successful_requests": self.unpriced_requests,
                "cost_estimate_complete": not self.unpriced_requests and not self.ledger.get("provider_errors"),
                "known_price_estimate_cny": self.actual_cost_cny,
                "cost_scope": "Known-price successful requests only; unpriced/failed charges are unknown."}

    def complete_cost_estimate(self):
        return self.actual_cost_cny if self.provider_statistics()["cost_estimate_complete"] else None

    def _query(self, messages, **kwargs):
        payload_bytes = len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
        if self.ledger.get("provider_failure"):
            self._stop_for_provider(self.ledger["provider_failure"])
        if self.ledger.get("completed") or self.ledger.get("blocked_auth") or payload_bytes > self.budget_config["max_messages_utf8_bytes"]:
            kind = "completed_batch" if self.ledger.get("completed") else "authentication_blocked" if self.ledger.get("blocked_auth") else "context_bytes"
            raise LimitsExceeded({"role": "exit", "content": "Stopped before request: " + kind,
                                  "extra": {"exit_status": "LimitsExceeded", "submission": "", "limit_kind": kind,
                                            "message_utf8_bytes": payload_bytes,
                                            "max_messages_utf8_bytes": self.budget_config["max_messages_utf8_bytes"]}})
        if kwargs or self._reservation is not None:
            raise RuntimeError("Cannot override budgeted request parameters or retry unsettled usage.")
        for index in range(self.provider_index, len(self.providers)):
            provider = self._activate_provider(index)
            try:
                self._reservation = self.budget.reserve(provider=provider.name)
            except BudgetExhausted:
                raise LimitsExceeded({"role": "exit", "content": "Shared CNY budget exhausted.",
                    "extra": {"exit_status": "LimitsExceeded", "submission": "", "limit_kind": "cost"}}) from None
            self.requests += 1
            self.ledger["attempts"] += 1
            self.ledger.setdefault("provider_attempts", []).append({"attempt": self.requests, "provider": provider.name})
            self.ledger_path.write_text(json.dumps(self.ledger, indent=2), encoding="utf-8")
            try:
                response = super()._query(messages, api_key=self._api_key, **kwargs)
                self._response_provider = provider
                return response
            except Exception as error:
                failure, diagnostic = classify_failure(error, provider, self.providers)
                self.budget.note_failure(self._reservation, failure)
                self._reservation = None
                self.ledger.setdefault("provider_errors", []).append(failure)
                can_switch = failure["failover_eligible"] and index + 1 < len(self.providers)
                if can_switch:
                    self.ledger.setdefault("provider_switches", []).append({"from": provider.name,
                        "to": self.providers[index + 1].name, "reason": failure["kind"], "attempt": self.requests})
                else:
                    if failure["kind"] == "authentication_failed":
                        self.ledger["blocked_auth"] = True
                    self.ledger["provider_failure"] = failure
                self.ledger_path.write_text(json.dumps(self.ledger, ensure_ascii=False, indent=2), encoding="utf-8")
                self.error_path.write_text(json.dumps({"type": type(error).__name__, "message": diagnostic,
                    "provider_failure": failure, "fallback_attempted_next": can_switch}, ensure_ascii=False, indent=2), encoding="utf-8")
                if not can_switch:
                    self._stop_for_provider(failure)

    @staticmethod
    def _stop_for_provider(failure):
        raise InterruptAgentFlow({"role": "exit", "content": failure["notice"], "extra": {
            "exit_status": "ProviderUnavailable", "submission": "", "provider_failure": failure}}) from None

    def _calculate_cost(self, response):
        usage = response.usage
        if usage is None or usage.prompt_tokens is None or usage.completion_tokens is None:
            raise RuntimeError("Missing token usage; stop instead of spending again.")
        # Official peak prices; deliberately ignore cache and off-peak discounts.
        if usage.prompt_tokens < 0 or usage.completion_tokens < 0:
            raise RuntimeError("Invalid token usage; retain reservation and stop.")
        provider = self._response_provider
        if provider is None or self._reservation is None:
            raise RuntimeError("Response has no associated provider request.")
        if not provider.official_pricing:
            self.budget.record_unpriced(self._reservation, {"prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens, "response_model": getattr(response, "model", None)})
            self._reservation = None
            self.unpriced_requests += 1
            self.usage.append({"prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens,
                "estimated_cost_cny": None, "provider": provider.name, "cost_status": "unknown_platform_price"})
            if usage.prompt_tokens > self.budget_config["max_prompt_tokens"] or usage.completion_tokens > self.budget_config["max_output_tokens"]:
                raise RuntimeError("Usage exceeded request token protection; stop for review.")
            return {"cost": 0.0, "cost_currency": "CNY", "cost_known": False, "provider": provider.name}
        exact_cost = (Decimal(usage.prompt_tokens) * Decimal(self.budget_config["conservative_input_cny_per_million"])
                      + Decimal(usage.completion_tokens) * Decimal(self.budget_config["conservative_output_cny_per_million"])) / Decimal(1_000_000)
        self.budget.settle(self._reservation, exact_cost)
        self._reservation = None
        cost = float(exact_cost)
        self.actual_cost_cny += cost
        self.usage.append({"prompt_tokens": usage.prompt_tokens,
                           "completion_tokens": usage.completion_tokens,
                           "provider": provider.name,
                           "estimated_cost_cny": cost})
        if usage.prompt_tokens > self.budget_config["max_prompt_tokens"] or usage.completion_tokens > self.budget_config["max_output_tokens"]:
            raise RuntimeError("Usage exceeded conservative reservation; stop for review.")
        return {"cost": cost, "cost_currency": "CNY"}

    def serialize(self):
        data = super().serialize()
        data["info"]["model_budget"] = {
            "currency": "CNY", **self.budget.snapshot(),
            "estimated_actual_cost_cny": self.complete_cost_estimate(), "usage": self.usage,
            "provider_routing": self.provider_statistics(),
        }
        return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="Make paid requests; requires user authorization.")
    args = parser.parse_args()
    settings = dotenv_values(ROOT / ".env", interpolate=False)
    required = ("DEEPSEEK_API_KEY", "DEEPSEEK_API_BASE_URL", "DEEPSEEK_CHAT_MODEL")
    if not all(settings.get(k) for k in required):
        raise SystemExit("Required DeepSeek settings missing.")
    if urlparse(settings["DEEPSEEK_API_BASE_URL"]).hostname != "api.deepseek.com":
        raise SystemExit("Pricing assumes official DeepSeek endpoint; review other endpoints first.")
    if settings["DEEPSEEK_CHAT_MODEL"] != "deepseek-flash":
        raise SystemExit("Pricing configured for Flash; review other models first.")
    bash = Path(r"D:\Git\bin\bash.exe")
    if not bash.is_file():
        raise SystemExit("Configured Git Bash executable not found.")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    work = ROOT / "examples" / "cart-empty" / "temp" / ("deepseek_" + stamp)
    work.mkdir(parents=True)
    (ROOT / "temp" / "bash-home").mkdir(parents=True, exist_ok=True)
    (work / "temp").mkdir()
    for name in ("cart.py", "test_cart.py"):
        shutil.copy2(ROOT / "examples" / "cart-empty" / name, work / name)
    env = GitBashEnvironment(work, bash)
    model = BudgetedDeepSeekModel(settings)
    baseline = env.execute({"command": "python -B -m unittest -v"})
    assert baseline["returncode"] != 0 and "FAILED (errors=1)" in baseline["output"]
    # These are real local commands, not a model request or a repair hint.
    probe = env.execute({"command": "printf 'bash-ok\\n'; python -B -c 'import sys; print(sys.prefix)'"})
    assert probe["returncode"] == 0 and "bash-ok" in probe["output"]
    assert str(ROOT / ".venv").lower() in probe["output"].lower()
    assert not model.contains_credentials(json.dumps(model.serialize()))
    payload = model._prepare_messages_for_api([{"role": "user", "content": "hello", "extra": {"local": True}}])
    assert "extra" not in payload[0] and model.requests == 0
    for messages, count in (([{"role": "user", "content": "x" * 17000}], 0),):
        model.requests = count
        try:
            model._query(messages)
        except LimitsExceeded:
            pass
        else:
            raise AssertionError("Pre-request guard did not stop execution.")
        assert model.requests == count
    model.requests = 0
    if not args.run:
        report = {"bash_commands": "passed", "virtualenv_python": "passed",
                  "original_task": "4 passed, 1 error", "model_config": "loaded",
                  "serialized_credentials": "absent", "external_requests": 0,
                  "planned_max_requests": None, "shared_budget": model.budget.snapshot()}
        report["provider_routing"] = model.provider_statistics()
        (ROOT / "temp" / "deepseek_preflight.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return
    result_dir = ROOT / "temp" / ("deepseek_result_" + stamp)
    result_dir.mkdir()
    agent = DefaultAgent(
        model, env, system_template="You are a helpful software engineering assistant. Use exactly one bash tool call per response.",
        instance_template=("Fix this issue: {{task}}\n"
                           "Work only in the current task directory. Do not delete files. Keep any helper files in temp/. "
                           "Do not modify test_cart.py. Inspect code, reproduce the failure, implement a minimal fix, "
                           "and run python -B -m unittest -v. "
                           "Every command runs in a fresh Git Bash shell on Windows with Python available. "
                           "When finished, submit using echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT by itself."),
        step_limit=0, cost_limit=model.budget.agent_cost_limit, wall_time_limit_seconds=300,
        output_path=result_dir / "trajectory.json",
    )
    status = agent.run("An empty cart raises an exception when calculating its total; retain behavior for non-empty carts.")
    verification = env.execute({"command": "python -B -m unittest -v"})
    tests_unchanged = (work / "test_cart.py").read_bytes() == (ROOT / "examples" / "cart-empty" / "test_cart.py").read_bytes()
    before = (ROOT / "examples" / "cart-empty" / "cart.py").read_text().splitlines(keepends=True)
    after = (work / "cart.py").read_text().splitlines(keepends=True)
    (result_dir / "repair.diff").write_text("".join(difflib.unified_diff(before, after, fromfile="a/cart.py", tofile="b/cart.py")), encoding="utf-8")
    summary = {"exit_status": status["exit_status"], "provider_failure": model.ledger.get("provider_failure"), "requests": model.requests,
               "provider_routing": model.provider_statistics(),
               "estimated_cost_cny": model.complete_cost_estimate(), "known_price_estimate_cny": model.actual_cost_cny, "usage": model.usage,
               "tests_passed": verification["returncode"] == 0 and tests_unchanged,
               "tests_unchanged": tests_unchanged, "verification_output": verification["output"],
               "scope": "Single synthetic teaching task; not SWE-bench or a performance comparison."}
    (result_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    model.ledger["completed"] = True
    model.ledger["note"] = "First teaching-task batch ended; do not spend remaining requests automatically."
    model.ledger_path.write_text(json.dumps(model.ledger, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("Review temporary results before promoting them to experiments/: " + str(result_dir))


if __name__ == "__main__":
    main()
