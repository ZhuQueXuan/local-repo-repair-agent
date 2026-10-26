"""One supervised task. Reuse existing Agent, accounting and independent grading."""
import argparse
import ctypes
import difflib
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

sys.dont_write_bytecode = True
from task_service_store import ROOT, DEFAULT_HOME, RunStore, now, worker_lock
from task_workspace import prepare_task, load_task, changed_files, package_root, digest
from run_task_batch import (RecordedModel, StatefulModel, TaskEnvironment, BoundedTaskEnvironment,
                            StatefulTaskEnvironment, DefaultAgent, StatefulAgent, RepositoryRetrieval,
                            evaluate, write_repair_patch)
from minisweagent.exceptions import InterruptAgentFlow
from deepseek_budget import SharedBudget, load_budget_config


class RunBudget(SharedBudget):
    def __init__(self, store, run_id):
        super().__init__(load_budget_config())
        self.store, self.run_id = store, run_id

    def reserve(self, *, provider=None):
        check_cancel(self.store, self.run_id)
        ticket = super().reserve(provider=provider)
        # Durable run-to-shared-ledger association before the HTTP call.
        self.store.event(self.run_id, "usage_reserved", {"ticket": ticket, "provider": provider})
        return ticket


def attach_process_lifetime():
    """On Windows a dead worker must not leave its Bash/test descendants running.

    The Job handle stays open until process exit. This is cleanup, not a sandbox.
    """
    if os.name != "nt":
        return None
    from ctypes import wintypes as w

    class Basic(ctypes.Structure):
        _fields_ = [("user", ctypes.c_int64), ("job", ctypes.c_int64), ("flags", w.DWORD),
                    ("minimum", ctypes.c_size_t), ("maximum", ctypes.c_size_t), ("active", w.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", ctypes.c_uint64 * 6),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.GetCurrentProcess.restype = w.HANDLE
    handle = kernel.CreateJobObjectW(None, None)
    settings = Extended()
    settings.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(settings), ctypes.sizeof(settings)) or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def check_cancel(store, run_id):
    if store.get(run_id)["cancel_requested"]:
        raise InterruptAgentFlow({"role": "exit", "content": "Cancellation requested", "extra": {"exit_status": "Cancelled", "submission": ""}})


def terminate_owned_process(process):
    """Stop only this worker's newly created subprocess and its descendants."""
    if process.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
        if process.poll() is None:
            process.kill()


class ServiceModelHooks:
    def __init__(self, *args, service_store, service_run_id, **kwargs):
        self.service_store, self.service_run_id = service_store, service_run_id
        super().__init__(*args, **kwargs)

    def query(self, messages):
        check_cancel(self.service_store, self.service_run_id)
        self.service_store.event(self.service_run_id, "model_request", {"next_attempt": self.requests + 1}, "repairer")
        try:
            result = super().query(messages)
            # The response is already accounted for. A cancel must prevent its action.
            check_cancel(self.service_store, self.service_run_id)
            return result
        finally:
            self.service_store.update(self.service_run_id, event="model_usage", actor="repairer", metrics=model_metrics(self))


class ServiceRawModel(ServiceModelHooks, RecordedModel):
    pass


class ServiceStatefulModel(ServiceModelHooks, StatefulModel):
    pass


class ServiceEnvironmentHooks:
    def __init__(self, *args, service_store, service_run_id, **kwargs):
        self.service_store, self.service_run_id = service_store, service_run_id
        super().__init__(*args, **kwargs)

    def execute(self, action, cwd="", *, timeout=None):
        check_cancel(self.service_store, self.service_run_id)
        self.service_store.event(self.service_run_id, "action_started", {"command": action["command"]}, "repairer")
        result = super().execute(action, cwd, timeout=timeout)
        self.service_store.event(self.service_run_id, "action_finished", {"returncode": result["returncode"], "preview": result["output"][:2000]}, "repairer")
        return result


class ServiceRawEnvironment(ServiceEnvironmentHooks, TaskEnvironment):
    pass


class ServiceStatefulEnvironment(ServiceEnvironmentHooks, StatefulTaskEnvironment):
    pass


def model_metrics(model):
    unpriced = sum(item.get("estimated_cost_cny") is None for item in model.usage)
    unknown = model.requests - len(model.usage) + unpriced
    return {"requests": model.requests, "successful_responses": len(model.usage),
            "prompt_tokens": sum(x["prompt_tokens"] for x in model.usage),
            "completion_tokens": sum(x["completion_tokens"] for x in model.usage),
            "known_price_estimate_cny": model.actual_cost_cny,
            "complete_cost_estimate_cny": model.complete_cost_estimate(),
            "unknown_attempts": unknown,
            "unknown_placeholder_cny": round(unknown * float(model.budget_config["reservation_per_attempt_cny"]), 6),
            "provider_routing": model.provider_statistics(), "cost_scope": "已知价格成功请求保守估算；未知占位不是已确认收费或平台余额"}


def freeze_inputs(task, output):
    source, manifest = load_task(task)
    paths = set((ROOT / "scripts").glob("*.py"))
    paths.update((ROOT / "configs").glob("*-requirements.txt"))
    paths.update(ROOT / "configs" / name for name in ("deepseek_budget.json", "provider_priority.json", "retrieval_delivery_trial_20261015.json"))
    paths.update((ROOT / "mini-swe-agent/src/minisweagent").rglob("*.py"))
    paths.update(ROOT / name for name in ("scripts/task_panel.html", "scripts/collaboration_panel.js", "scripts/launch_task_panel.pyw", "启动任务面板.cmd"))
    paths.update(source / "repo" / name for name in manifest["source_files_sha256"])
    paths.update(source / name for name in (*manifest["task_files_sha256"], "manifest.json"))
    listing = {}
    for path in sorted(paths):
        relative = path.relative_to(ROOT).as_posix()
        listing[relative] = digest(path)
        destination = output / "frozen_inputs" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    (output / "inputs.json").write_text(json.dumps({"version": 1, "files_sha256": listing}, indent=2), encoding="utf-8")
    return listing


def collect_patch(task, work, output, manifest):
    source, _ = load_task(task)
    changed = changed_files(work, manifest)
    unsupported = [name for name in changed if not name.startswith(manifest["source_prefix"]) or name not in manifest["source_files_sha256"] or not (work / name).is_file()]
    if unsupported:
        raise RuntimeError("Unsupported change; first version accepts existing implementation files only: " + ", ".join(unsupported))
    for relative, expected in manifest["task_files_sha256"].items():
        candidate = work / ("TASK.md" if relative == "issue.md" else "task_checks/test_behavior.py")
        if not candidate.is_file() or digest(candidate) != expected:
            raise RuntimeError("Protected task description/check changed")
    patch = []
    for name in changed:
        patch.extend(difflib.unified_diff((source / "repo" / name).read_text(encoding="utf-8").splitlines(keepends=True),
                    (work / name).read_text(encoding="utf-8").splitlines(keepends=True), fromfile="a/" + name, tofile="b/" + name))
    write_repair_patch(output / "repair.diff", patch)
    return changed


def run_one(store, run_id):
    record = store.update(run_id, expected={"queued"}, event="worker_claimed", status="running", stage="preparing", started=now(), worker_pid=os.getpid())
    if record is None:
        return
    output = store.home / "runs" / run_id
    output.mkdir(parents=True, exist_ok=False)
    scratch = ROOT / "temp/task_service" / run_id
    scratch.mkdir(parents=True, exist_ok=True)
    model, work, manifest, env, status = None, None, None, None, {}
    begin = time.monotonic()
    artifacts = {"run_config.json": str(output / "run_config.json"), "inputs.json": str(output / "inputs.json")}
    result = {"status": "failed", "repair_result": "unknown", "reason": "运行未正常收尾"}

    def stage(name):
        check_cancel(store, run_id)
        store.update(run_id, event="stage", stage=name)

    def grade(name, checkout):
        stage(name)
        target = scratch / name / "report.json"
        result = evaluate(record["task"], checkout, target)
        shutil.copytree(target.parent, output / name)
        artifacts[name + ".json"] = str(output / name / "report.json")
        return result

    try:
        (output / "run_config.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        frozen = freeze_inputs(record["task"], output)
        work, manifest = prepare_task(record["task"])
        store.update(run_id, event="workspace_ready", workspace=str(work))
        baseline = grade("baseline", work)
        if baseline["issue_checks"]["successful"] or not baseline["upstream_targeted"]["successful"]:
            raise RuntimeError("Expected reproduced issue with passing targeted upstream tests")
        if record["kind"] == "replay":
            stage("replaying_patch")
            original = ROOT / "experiments/retrieval_delivery_trial_20261015" / record["task"] / "retrieval_on_change/repair.diff"
            subprocess.run(["git", "-C", str(work), "apply", "--", str(original)], check=True, capture_output=True)
            store.event(run_id, "archived_patch_applied", {"source": original.relative_to(ROOT).as_posix(), "new_model_requests": 0}, "replay")
            status = {"exit_status": "OfflinePatchReplay"}
        else:
            stage("preflight")
            bash = Path(os.environ.get("MINI_TASK_BASH", r"D:\Git\bin\bash.exe"))
            if not bash.is_file():
                raise RuntimeError("Git Bash not found; set MINI_TASK_BASH to its executable")
            import_root = package_root(work, manifest) if manifest.get("import_root") else None
            probe_env = TaskEnvironment(work, bash, python_import_root=import_root)
            imported = probe_env.execute({"command": "python -B -c " + shlex.quote("import importlib; print(importlib.import_module(" + repr(manifest["package"]) + ").__file__)")})
            if imported["returncode"] or not Path(imported["output"].strip()).resolve().is_relative_to(work):
                raise RuntimeError("Package import escaped task checkout")
            probe = probe_env.execute({"command": "python -B -c 'raise SystemExit(7)' | tail -1"})
            if probe["returncode"] == 0:
                raise RuntimeError("Pipeline failure propagation is unavailable")
            hooks = {"service_store": store, "service_run_id": run_id}
            configuration = record["configuration"]
            if configuration:
                env = ServiceStatefulEnvironment(work, bash, source_files=list(manifest["source_files_sha256"]), state_policy=configuration["task_state"], python_import_root=import_root, **configuration["tool_output_budget"], **hooks)
            else:
                env = ServiceRawEnvironment(work, bash, python_import_root=import_root, **hooks)
            retrieval = RepositoryRetrieval(work, manifest["source_files_sha256"], source_prefix=manifest["source_prefix"], query=(ROOT / "tasks" / record["task"] / "issue.md").read_text(encoding="utf-8"), **configuration["repository_retrieval"]) if configuration.get("repository_retrieval") else None
            from dotenv import dotenv_values
            settings = dotenv_values(ROOT / ".env", interpolate=False)
            options = {"ledger_path": output / "ledger.json", "error_path": output / "request_error.json", "repository_retrieval": retrieval, "budget": RunBudget(store, run_id), **hooks}
            model = ServiceStatefulModel(settings, task_state=env.task_state, context_policy=configuration["context_budget"], **options) if configuration else ServiceRawModel(settings, **options)
            if configuration.get("archive_assistant_content"):
                model.context_budget.enable_assistant_archives(work)
            issue_command = manifest.get("agent_issue_command", "python -B -m unittest discover -s task_checks -v")
            instruction = ("Fix this issue: {{task}}\nWork only in this checkout. Do not access parent directories, unrelated projects, the web, or install dependencies. "
                "Do not delete files or edit tests/, task_checks/, TASK.md, or metadata. Change only existing implementation files. "
                "Put temporary helpers in temp/. Search symbols and read short line ranges. Commands use fresh Git Bash on Windows with pipeline failures propagated. "
                "Inspect, reproduce, repair, then run both:\n" + manifest["agent_test_command"] + "\n" + issue_command +
                "\nWhen finished, run echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT by itself.")
            if configuration:
                instruction += "\nA bounded program-recorded state is provided. Model notes are unverified. When your diagnosis changes, write string fields hypothesis and next_action to temp/task-state/notes.json. Use evidence to proceed to concrete reproduction, repair and validation."
            agent_class = StatefulAgent if configuration else DefaultAgent
            agent = agent_class(model, env, system_template="You are a helpful software engineering assistant. Use exactly one bash tool call per response.", instance_template=instruction,
                                step_limit=0, cost_limit=model.budget.agent_cost_limit, wall_time_limit_seconds=300, output_path=output / "trajectory.json")
            stage("agent")
            agent_begin = time.monotonic()
            status = agent.run((ROOT / "tasks" / record["task"] / "issue.md").read_text(encoding="utf-8"))
            store.update(run_id, event="agent_ended", agent_exit=status.get("exit_status"), elapsed_agent_seconds=round(time.monotonic() - agent_begin, 3))
            artifacts["trajectory.json"] = str(output / "trajectory.json")
        changed = collect_patch(record["task"], work, output, manifest)
        artifacts["repair.diff"] = str(output / "repair.diff")
        check_cancel(store, run_id)
        verification = grade("verification", work)
        before_full, after_full = baseline["full_upstream_suite"], verification["full_upstream_suite"]
        new_failures = sorted(set(after_full["failures"] + after_full["errors"]) - set(before_full["failures"] + before_full["errors"]))
        passed = verification["task_tests_passed"] and not new_failures and bool(changed)
        if passed:
            replay, _ = prepare_task(record["task"])
            subprocess.run(["git", "-C", str(replay), "apply", "--", str(output / "repair.diff")], check=True, capture_output=True)
            checked = grade("patch_verification", replay)
            full = checked["full_upstream_suite"]
            passed = checked["task_tests_passed"] and not (set(full["failures"] + full["errors"]) - set(before_full["failures"] + before_full["errors"]))
        if any(digest(ROOT / name) != value for name, value in frozen.items()):
            raise RuntimeError("Frozen run input changed")
        reason = (status.get("provider_failure") or {}).get("notice", "")
        if status.get("exit_status") == "LimitsExceeded":
            reason = "Agent触发运行保护：" + str(status.get("limit_kind", "limits"))
        elif status.get("exit_status") == "NoProgress":
            reason = "Agent因持续重复读取停止；修复成绩由独立检查另行判断。"
        result = {"status": "finished", "repair_result": "passed" if passed else "failed", "changed_files": changed,
                  "new_extended_failures": new_failures, "reason": reason, "agent_exit": status.get("exit_status"), "limit_kind": status.get("limit_kind")}
    except InterruptAgentFlow:
        result = {"status": "cancelled", "repair_result": "not_run", "agent_exit": status.get("exit_status"), "reason": "已取消；不再启动下一动作或请求，已有用量保留。"}
    except Exception as error:
        # Never serialize an arbitrary provider exception or private settings.
        result = {"status": "failed", "repair_result": "unknown", "agent_exit": status.get("exit_status"), "reason": "阶段处理失败：" + type(error).__name__ + "。已保留事件和现有文件，请查看本地诊断。"}
        diagnostic = str(error)
        if model is not None:
            from provider_priority import redact
            diagnostic = redact(diagnostic, model.providers)
        (scratch / "diagnostic.txt").write_text(diagnostic, encoding="utf-8")
    finally:
        if work is not None and manifest is not None and not (output / "repair.diff").exists():
            try:
                result["changed_files"] = collect_patch(record["task"], work, output, manifest)
                artifacts["repair.diff"] = str(output / "repair.diff")
            except Exception:
                store.event(run_id, "patch_collection_failed", {"note": "保留工作副本；没有可确认的合规补丁"})
        if work is not None:
            for name in ("tool-output", "task-state", "context-history", "repository-retrieval"):
                source = work / "temp" / name
                if source.is_dir():
                    shutil.copytree(source, output / name, dirs_exist_ok=True)
        if model is not None:
            model.ledger.update(completed=True, run_id=run_id, note="Service run ended; shared accounting stays cumulative.")
            (output / "ledger.json").write_text(json.dumps(model.ledger, ensure_ascii=False, indent=2), encoding="utf-8")
            metrics = model_metrics(model)
            artifacts["ledger.json"] = str(output / "ledger.json")
        else:
            metrics = {"requests": 0, "successful_responses": 0, "prompt_tokens": 0, "completion_tokens": 0,
                       "known_price_estimate_cny": 0, "complete_cost_estimate_cny": 0, "cost_scope": "本次未调用模型"}
        metrics["total_elapsed_seconds"] = round(time.monotonic() - begin, 3)
        if store.get(run_id)["cancel_requested"] and result["status"] == "finished":
            result.update(status="cancelled", reason="执行与验收已收尾；期间收到取消请求，已有验收结果保留。")
        summary = {"run_id": run_id, "task_id": manifest["task_id"] if manifest else record["task"], "variant": record["profile"],
                   "mode": record["kind"], **result, "metrics": metrics, "scope": record["scope"]}
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        artifacts["summary.json"] = str(output / "summary.json")
        if model is not None and any(model.contains_credentials(p.read_text(encoding="utf-8", errors="replace")) for p in output.rglob("*") if p.is_file()):
            store.update(run_id, event="sensitive_artifact_blocked", status="failed", stage="failed", ended=now(), reason="输出凭据检查未通过，成果暂不开放", metrics=metrics, artifacts={})
        else:
            hashes = {p.relative_to(output).as_posix(): digest(p) for p in output.rglob("*") if p.is_file()}
            (output / "artifact_sha256.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")
            artifacts["artifact_sha256.json"] = str(output / "artifact_sha256.json")
            store.update(run_id, event="run_ended", stage="done", ended=now(), metrics=metrics, artifacts=artifacts, **result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--collaboration", action="store_true", help="Explicit stage-C worker; temp store only, no panel default change")
    parser.add_argument("--panel-store", action="store_true", help="Explicit access to the standard panel collaboration database")
    args = parser.parse_args()
    if args.collaboration:
        from collaboration_store import CollaborationStore
        store = CollaborationStore(args.home, allow_production=args.panel_store)
    else:
        store = RunStore(args.home)
    with worker_lock(store.home) as locked:
        if not locked:
            return
        try:
            lifetime = attach_process_lifetime()
            if args.collaboration:
                from collaboration_live import run_live_worker
                result = run_live_worker(store, args.run_id)
                if result["status"] != "finished":
                    raise SystemExit(1)
            else:
                run_one(store, args.run_id)
        except Exception as error:
            store.update(args.run_id, event="worker_error", status="failed", stage="failed", ended=now(), repair_result="unknown", reason="执行进程处理失败：" + type(error).__name__ + "；保留现有文件，不自动重试。")
            raise SystemExit(1)


if __name__ == "__main__":
    main()
