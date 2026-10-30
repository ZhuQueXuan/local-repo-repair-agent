"""Loopback-only task panel and serial process supervisor. No new dependency."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlparse
import webbrowser
import zipfile

sys.dont_write_bytecode = True
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")
from task_service_store import ROOT, DEFAULT_HOME, RunStore, PROFILES, catalog, import_history, worker_lock, now
from release_info import release_identity
from execution_backend import terminate_owned_process
from collaboration_panel import create_panel_run, collaboration_view, candidate_view, evidence_view, registered_artifact, import_sealed_collaboration
from collaboration_store import CollaborationStore, StateConflict
from task_cards import card_view, update_card, TaskCardConflict
from harness_checkpoints import checkpoint_view, request_pause, continue_checkpoint, CheckpointConflict


class Supervisor:
    def __init__(self, store):
        self.store, self.stop = store, threading.Event()
        self.process, self.log = None, None
        self.process_run_id = None
        self.process_started = self.stage_started = self.cancel_started = None
        self.observed_stage = None

    def watchdog_reason(self, row):
        current = time.monotonic()
        if row.get("cancel_requested"):
            if self.cancel_started is None:
                self.cancel_started = current
            if current - self.cancel_started >= 50:
                return "取消等待超过50秒，已停止本面板拥有的执行器；在途请求结果未知，费用占位和现有文件保留，不自动重发。"
        if row["stage"] != self.observed_stage:
            self.observed_stage, self.stage_started = row["stage"], current
        collaboration = row.get("collaboration_policy", {})
        total_limit = collaboration.get("parent_seconds", 1140) + 60
        if row.get('kind') == 'analysis':
            total_limit = (480 if row.get('analysis_mode') == 'analysis_review' else 300) + 60
        limits = {"queued": 300, "preparing": 90, "baseline": 250, "preflight": 90,
                  "agent": 345, "analysing": 345, "reviewing_analysis": 225, "replaying_patch": 45, "verification": 250, "patch_verification": 250}
        limits.update({phase: seconds + 45 for phase, seconds in collaboration.get("role_seconds", {}).items()})
        stage_limit = limits.get(row["stage"])
        if (self.process_started is not None and current - self.process_started > total_limit
                or stage_limit is not None and current - self.stage_started > stage_limit):
            return "执行器超过独立等待上限，已停止该进程；当前结果未知，现有文件及费用记录保留，不自动重发。"
        return None

    def stop_owned_worker(self, row, reason):
        terminate_owned_process(self.process)
        self.store.event(row["id"], "worker_watchdog_stopped", {"reason": reason})
        if row["kind"] == "collaboration_live":
            self.store.stop(row["id"], "cancelled" if row["cancel_requested"] else "failed", reason)
        else:
            fields = {}
            if row['kind'] == 'analysis':
                from analysis_runtime import interrupted_analysis_fields
                fields = interrupted_analysis_fields(row, 'cancelled' if row['cancel_requested'] else 'failed')
            self.store.update(row["id"], expected={"queued", "running", "cancel_requested"},
                status="cancelled" if row["cancel_requested"] else "failed", stage="done", ended=now(),
                repair_result="unknown", reason=reason, **fields)

    def failed_before_claim(self, run_id, reason):
        row = self.store.get(run_id)
        if row["status"] != "queued":
            return
        if row["kind"] == "collaboration_live":
            with self.store.transaction() as db:
                parent, _ = self.store._load(db, run_id)
                if parent["status"] == "queued":
                    self.store._stop(db, run_id, "failed", reason)
                    self.store.emit(db, run_id, "worker_start_failed", {"reason": reason})
        else:
            self.store.update(run_id, expected={"queued"}, event="worker_start_failed",
                status="failed", stage="failed", ended=now(), reason=reason)

    def tick(self):
        if self.process is not None:
            if self.process.poll() is None:
                if self.process_run_id is not None:
                    row = self.store.get(self.process_run_id)
                    reason = self.watchdog_reason(row)
                    if reason:
                        self.stop_owned_worker(row, reason)
                return
            exit_code = self.process.returncode
            self.log.close()
            if self.process_run_id is not None:
                self.failed_before_claim(self.process_run_id,
                    "执行器在接手任务前退出（退出码 " + str(exit_code) + "）；未自动重试，请查看启动日志或另建任务。")
            self.process = self.log = None
            self.process_run_id = None
            self.process_started = self.stage_started = self.cancel_started = None
            self.observed_stage = None
        with worker_lock(self.store.home) as acquired:
            if not acquired:
                return  # A worker from an earlier server process still owns its work.
            self.store.reconcile()
            queued = [row for row in self.store.list() if row["status"] == "queued" and row["kind"] in {"live", "replay", "collaboration_live", "analysis"}]
            if not queued:
                return
            row = min(queued, key=lambda r: r["created"])
            log_dir = ROOT / "temp/task_service" / row["id"]
            log_dir.mkdir(parents=True, exist_ok=True)
            self.log = (log_dir / "worker.log").open("a", encoding="utf-8")
            command = [str(ROOT / ".venv/Scripts/python.exe"), "-B", str(ROOT / "scripts/task_service_worker.py"), "--run-id", row["id"], "--home", str(self.store.home)]
            if row["kind"] == "collaboration_live":
                command += ["--collaboration", "--panel-store"]
            elif row["kind"] == "analysis":
                command += ["--analysis"]
            elif row["kind"] not in {"live", "replay"}:
                return  # Never dispatch historical/mock fixture records as live work.
            try:
                self.process = subprocess.Popen(command, cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                self.process_run_id = row["id"]
                self.process_started = time.monotonic()
            except OSError as error:
                self.log.close()
                self.log = None
                self.failed_before_claim(row["id"], "无法启动执行器：" + type(error).__name__ + "。未调用模型，请检查项目环境后另建任务。")

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as error:
                # Avoid a silent dead scheduler. Detailed process logs stay in temp.
                print("Task supervisor: " + type(error).__name__, flush=True)
            self.stop.wait(0.5)


def make_handler(store, token, port):
    release = release_identity()
    operations = threading.Lock()
    closing = False
    last_execution_report = None
    from dotenv import dotenv_values
    # Authentication-only values never reach HTTP. Check complete responses,
    # including DB objects/events and registered artifacts, before previewing.
    sensitive = [v for k, v in dotenv_values(ROOT / ".env", interpolate=False).items() if "KEY" in k and v]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, value, content_type="application/json; charset=utf-8", *, headers=None):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
            if any(secret.encode() in body for secret in sensitive):
                status, content_type = 409, "application/json; charset=utf-8"
                body = json.dumps({"error": "内容凭据检查未通过，预览已阻断。"}, ensure_ascii=False).encode()
                headers = None
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def allowed(self, api=True):
            if self.headers.get("Host") != "127.0.0.1:" + str(port):
                self.send(403, {"error": "Only the local panel address is accepted"})
                return False
            origin = self.headers.get("Origin")
            if origin and origin != "http://127.0.0.1:" + str(port):
                self.send(403, {"error": "Cross-origin requests are disabled"})
                return False
            if api and not secrets.compare_digest(self.headers.get("X-Task-Token", ""), token):
                self.send(403, {"error": "Refresh the local panel before making this request"})
                return False
            return True

        def do_GET(self):
            route = urlparse(self.path)
            if not self.allowed(api=route.path not in {"/", "/experiments", "/analysis-panel.js", "/api/health", "/collaboration-panel.js", "/task-card-panel.js", "/checkpoint-panel.js", "/capability-panel.js", "/execution-panel.js", "/model-profile-panel.js"}):
                return
            try:
                if route.path == "/":
                    html = (ROOT / "scripts/analysis_panel.html").read_text(encoding="utf-8").replace("__TASK_TOKEN__", token).replace("__RELEASE_VERSION__", release["version"])
                    self.send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                elif route.path == "/experiments":
                    html = (ROOT / "scripts/task_panel.html").read_text(encoding="utf-8").replace("__TASK_TOKEN__", token).replace("__RELEASE_VERSION__", release["version"])
                    self.send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                elif route.path == "/analysis-panel.js":
                    self.send(200, (ROOT / "scripts/analysis_panel.js").read_bytes(), "text/javascript; charset=utf-8")
                elif route.path.startswith('/api/sources/'):
                    from analysis_sources import load_source, source_bytes
                    source_id = route.path.split('/')[-1]
                    source = load_source(store.home, source_id)
                    query = parse_qs(route.query)
                    if 'path' in query:
                        path = query['path'][0]
                        lines = source_bytes(store.home, source, path).decode('utf-8-sig').splitlines()
                        start = max(1, int(query.get('start', ['1'])[0]))
                        end = min(len(lines), start + 79)
                        content = '\n'.join(lines[start-1:end])
                        if len(content.encode()) > 16000:
                            raise ValueError('预览行段过长，请选择较短源码文件。')
                        self.send(200, {'path': path, 'start': start, 'end': end, 'content': content, 'file': source['files'][path]})
                    else:
                        self.send(200, source)
                elif route.path == "/collaboration-panel.js":
                    self.send(200, (ROOT / "scripts/collaboration_panel.js").read_bytes(), "text/javascript; charset=utf-8")
                elif route.path == "/task-card-panel.js":
                    self.send(200, (ROOT / "scripts/task_card_panel.js").read_bytes(), "text/javascript; charset=utf-8")
                elif route.path == "/checkpoint-panel.js":
                    self.send(200, (ROOT / "scripts/checkpoint_panel.js").read_bytes(), "text/javascript; charset=utf-8")
                elif route.path == "/capability-panel.js":
                    self.send(200, (ROOT / "scripts/capability_panel.js").read_bytes(), "text/javascript; charset=utf-8")
                elif route.path == "/execution-panel.js":
                    self.send(200, (ROOT / "scripts/execution_panel.js").read_bytes(), "text/javascript; charset=utf-8")
                elif route.path == "/model-profile-panel.js":
                    self.send(200, (ROOT / "scripts/model_profile_panel.js").read_bytes(), "text/javascript; charset=utf-8")
                elif route.path == "/api/model-profiles":
                    from model_profiles import profile_catalog
                    self.send(200,profile_catalog())
                elif route.path == "/api/execution-environment":
                    from execution_runtime import runtime_report
                    self.send(200, last_execution_report or runtime_report())
                elif route.path == "/api/health":
                    self.send(200, {"product": "mini-swe-task-panel", "status": "ready", **release})
                elif route.path == "/api/catalog":
                    self.send(200, {"tasks": catalog(), "profiles": PROFILES, "collaboration_available": isinstance(store, CollaborationStore), "collaboration_policy_version": 1})
                elif route.path == "/api/runs":
                    self.send(200, store.list())
                elif route.path.startswith("/api/runs/"):
                    parts = route.path.split("/")
                    run_id = parts[3]
                    row = store.get(run_id)
                    if len(parts) == 4:
                        self.send(200, row)
                    elif parts[4] == "events" and len(parts) == 5:
                        self.send(200, store.events(run_id, max(0, int(parse_qs(route.query).get("after", [0])[0]))))
                    elif parts[4] == "collaboration" and len(parts) == 5:
                        self.send(200, collaboration_view(store, run_id))
                    elif parts[4] == "task-card" and len(parts) == 5:
                        self.send(200, card_view(store, run_id))
                    elif parts[4] == "checkpoint" and len(parts) == 5:
                        self.send(200, checkpoint_view(store, run_id))
                    elif parts[4] in {"candidates", "evidence"} and len(parts) == 6:
                        reader = candidate_view if parts[4] == "candidates" else evidence_view
                        self.send(200, reader(store, run_id, unquote(parts[5])))
                    elif parts[4] == "adjudications" and len(parts) == 7:
                        from collaboration_adjudication import read_proof
                        name = unquote(parts[6])
                        raw = read_proof(store, run_id, unquote(parts[5]), name)
                        self.send(200, raw, "application/zip" if name == "proof.zip" else "application/json; charset=utf-8")
                    elif parts[4] == "artifacts" and len(parts) == 6:
                        name = unquote(parts[5])
                        raw = registered_artifact(store, row, name)
                        if parse_qs(route.query).get("download") == ["1"]:
                            import hashlib
                            self.send(200, raw, "application/octet-stream", headers={
                                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(Path(name).name, safe=""),
                                "X-Content-SHA256": hashlib.sha256(raw).hexdigest()})
                        else:
                            self.send(200, raw[:256000] + (b"\n[preview truncated; use full download]" if len(raw) > 256000 else b""), "text/plain; charset=utf-8")
                    else:
                        self.send(404, {"error": "Unknown route"})
                else:
                    self.send(404, {"error": "Unknown route"})
            except (KeyError, FileNotFoundError):
                self.send(404, {"error": "Record or artifact is not available"})
            except (ValueError, TypeError, OSError):
                self.send(400, {"error": "Invalid request"})

        def do_POST(self):
            nonlocal closing, last_execution_report
            if not self.allowed():
                return
            try:
                route = urlparse(self.path).path
                if route == '/api/sources/upload':
                    from analysis_sources import MAX_UPLOAD, import_source
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= MAX_UPLOAD:
                        raise ValueError('上传内容为空或超过20MiB。')
                    name = parse_qs(urlparse(self.path).query).get('name', [''])[0]
                    self.connection.settimeout(30)
                    raw = self.rfile.read(size)
                    if len(raw) != size:
                        raise ValueError('上传未完成，请重新选择文件。')
                    with operations:
                        if closing:
                            self.send(503, {'error': '服务正在停止，请重开后导入。'})
                        else:
                            self.send(201, import_source(store.home, name, raw, secrets=sensitive))
                    return
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= (262144 if route == '/api/analysis' else 8192):
                    raise ValueError("Request is empty or too large")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError("Expected a JSON object")
                if any(secret in json.dumps(body, ensure_ascii=False) for secret in sensitive):
                    raise ValueError("请求中含有已配置的凭据，未保存或执行。")
                with operations:
                    if closing:
                        self.send(503, {"error": "面板服务正在停止，请重新启动后操作。"})
                    elif route == "/api/shutdown":
                        if any(row["status"] in {"queued", "running", "cancel_requested"} for row in store.list()):
                            self.send(409, {"error": "还有未结束的任务，请先取消或等待完成，再停止面板服务。"})
                        else:
                            closing = True
                            self.send(200, {"status": "stopping"})
                            threading.Thread(target=self.server.shutdown, daemon=True).start()
                    elif route == "/api/execution-environment/inspect":
                        if body:raise ValueError("执行环境检查只接受空对象，不接受路径或命令。")
                        from execution_runtime import runtime_report
                        last_execution_report = runtime_report(probe=True)
                        self.send(200, last_execution_report)
                    elif route == '/api/sources/github':
                        if set(body) - {'url', 'ref'}:
                            raise ValueError('仓库导入字段不正确。')
                        from analysis_sources import fetch_github
                        self.send(201, fetch_github(store.home, body.get('url'), body.get('ref', ''), secrets=sensitive))
                    elif route == '/api/analysis':
                        from analysis_runtime import create_analysis
                        self.send(201, create_analysis(store, body))
                    elif route == "/api/runs":
                        self.send(201, create_panel_run(store, body))
                    elif route.startswith("/api/runs/") and route.endswith("/cancel") and len(route.split("/")) == 5:
                        self.send(200, store.cancel(route.split("/")[3]))
                    elif route.startswith("/api/runs/") and route.endswith("/task-card") and len(route.split("/")) == 5:
                        self.send(200, update_card(store, route.split("/")[3], body))
                    elif route.startswith("/api/runs/") and route.endswith("/pause") and len(route.split("/")) == 5:
                        if body:
                            raise ValueError("暂停请求不接受额外字段。")
                        self.send(200, request_pause(store, route.split("/")[3]))
                    elif route.startswith("/api/runs/") and route.endswith("/continue") and len(route.split("/")) == 5:
                        self.send(201, continue_checkpoint(store, route.split("/")[3], body))
                    else:
                        self.send(404, {"error": "Unknown route"})
            except (TaskCardConflict, CheckpointConflict) as error:
                self.send(409, {"error": str(error)})
            except StateConflict:
                self.send(409, {"error": "请求身份与已有内容冲突；请刷新查看原任务，不自动重发。"})
            except (KeyError, TypeError, ValueError) as error:
                self.send(400, {"error": str(error)[:300]})
            except (OSError, zipfile.BadZipFile):
                self.send(400, {'error': '导入或读取失败，请检查文件、网络和本机目录权限。'})

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    store = CollaborationStore(args.home, allow_production=True)
    with worker_lock(store.home, "server.lock") as locked:
        if not locked:
            if args.open:
                webbrowser.open("http://127.0.0.1:" + str(args.port))
            return
        import_history(store)
        import_sealed_collaboration(store)
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store, secrets.token_urlsafe(32), args.port))
        supervisor = Supervisor(store)
        thread = threading.Thread(target=supervisor.run, daemon=True)
        thread.start()
        print("任务面板：http://127.0.0.1:" + str(args.port) + "；默认离线，真实任务须在面板主动启动。", flush=True)
        if args.open:
            webbrowser.open("http://127.0.0.1:" + str(args.port))
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
        finally:
            supervisor.stop.set()
            server.server_close()
            # A running worker owns its OS lock and can finish after the manager closes.


if __name__ == "__main__":
    main()
