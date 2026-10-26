"""Minimal real API probes; no repository content or generated command execution."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
from dotenv import dotenv_values
from minisweagent.exceptions import InterruptAgentFlow
from run_deepseek_smoke import BudgetedDeepSeekModel, ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="Perform minimal user-authorized API connection checks.")
    args = parser.parse_args()
    settings = dotenv_values(ROOT / ".env", interpolate=False)
    directory = ROOT / "temp" / ("provider_connection_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    directory.mkdir()
    cases = []
    backup_verified = False
    for label in ["primary_with_fallback", "backup_direct"]:
        if label == "backup_direct" and backup_verified:
            break  # A real fallback response already verified the backup; do not repeat it.
        selected = settings if label == "primary_with_fallback" else {k: v for k, v in settings.items() if k != "BUPT_API_KEY"}
        model = BudgetedDeepSeekModel(selected, ledger_path=directory / (label + "_ledger.json"),
                                     error_path=directory / (label + "_error.json"))
        model.config.model_kwargs["max_tokens"] = 128
        model.config.model_kwargs["timeout"] = 20
        report = {"probe": label, "live_requests_enabled": args.run,
                  "configured_priority": model.provider_statistics()["priority"]}
        if args.run:
            try:
                message = model.query([
                    {"role": "system", "content": "This is an API connectivity test. Reply with exactly one bash tool call."},
                    {"role": "user", "content": "Return a bash tool call whose command is printf 'provider-ok'. No commands will be executed."}])
                # Only check parsing; never execute any generated command.
                report.update(status="api_and_toolcall_verified", actions_parsed=len(message["extra"]["actions"]),
                    response_model=message["extra"]["response"].get("model"))
                if model.providers[model.provider_index].name == "deepseek_official":
                    backup_verified = True
            except InterruptAgentFlow as error:
                report.update(status="provider_unavailable", failure=error.messages[0].get("extra", {}).get("provider_failure"))
            except Exception as error:
                # Avoid provider bodies or credentials in terminal/log output.
                report.update(status="probe_not_verified", error_type=type(error).__name__)
            finally:
                model.ledger["completed"] = True
                model.ledger_path.write_text(json.dumps(model.ledger, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            report["status"] = "configuration_only_no_api_request"
        report.update(requests=model.requests, usage=model.usage, provider_routing=model.provider_statistics(),
                      shared_budget=model.budget.snapshot())
        assert not model.contains_credentials(json.dumps(report, ensure_ascii=False))
        cases.append(report)
        (directory / "summary.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    # Scan every receipt/error, with both keys even for the backup-only probe.
    secrets = [settings[k] for k in ["BUPT_API_KEY", "DEEPSEEK_API_KEY"] if settings.get(k)]
    assert not any(secret.encode() in file.read_bytes() for file in directory.iterdir() if file.is_file() for secret in secrets)
    print("Temporary results: " + str(directory), flush=True)
    return 0 if all(case["status"] in {"api_and_toolcall_verified", "configuration_only_no_api_request"} for case in cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
