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
from urllib.parse import parse_qs, unquote, urlparse
import webbrowser

sys.dont_write_bytecode = True
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")
from task_service_store import ROOT, DEFAULT_HOME, RunStore, PROFILES, catalog, import_history, worker_lock


class Supervisor:
    def __init__(self, store):
        self.store, self.stop = store, threading.Event()
        self.process, self.log = None, None

    def tick(self):
        if self.process is not None:
            if self.process.poll() is None:
                return
            self.log.close()
            self.process = self.log = None
        with worker_lock(self.store.home) as acquired:
            if not acquired:
                return  # A worker from an earlier server process still owns its work.
            self.store.reconcile()
            queued = [row for row in self.store.list() if row["status"] == "queued"]
            if not queued:
                return
            row = min(queued, key=lambda r: r["created"])
            log_dir = ROOT / "temp/task_service" / row["id"]
            log_dir.mkdir(parents=True, exist_ok=True)
            self.log = (log_dir / "worker.log").open("a", encoding="utf-8")
            self.process = subprocess.Popen([str(ROOT / ".venv/Scripts/python.exe"), "-B", str(ROOT / "scripts/task_service_worker.py"), "--run-id", row["id"], "--home", str(self.store.home)],
                                            cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT,
                                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as error:
                # Avoid a silent dead scheduler. Detailed process logs stay in temp.
                print("Task supervisor: " + type(error).__name__, flush=True)
            self.stop.wait(0.5)


def make_handler(store, token, port):
    operations = threading.Lock()
    closing = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, value, content_type="application/json; charset=utf-8"):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
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
            if not self.allowed(api=route.path not in {"/", "/api/health"}):
                return
            try:
                if route.path == "/":
                    html = (ROOT / "scripts/task_panel.html").read_text(encoding="utf-8").replace("__TASK_TOKEN__", token)
                    self.send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                elif route.path == "/api/health":
                    self.send(200, {"product": "mini-swe-task-panel", "status": "ready"})
                elif route.path == "/api/catalog":
                    self.send(200, {"tasks": catalog(), "profiles": PROFILES})
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
                    elif parts[4] == "artifacts" and len(parts) == 6:
                        name = unquote(parts[5])
                        path = Path(row["artifacts"][name]).resolve()
                        if not any(path.is_relative_to(base.resolve()) for base in (ROOT / "experiments", store.home / "runs")):
                            raise ValueError("Artifact is outside the allowed result directories")
                        raw = path.read_bytes()
                        self.send(200, raw[:256000] + (b"\n[preview truncated]" if len(raw) > 256000 else b""), "text/plain; charset=utf-8")
                    else:
                        self.send(404, {"error": "Unknown route"})
                else:
                    self.send(404, {"error": "Unknown route"})
            except (KeyError, FileNotFoundError):
                self.send(404, {"error": "Record or artifact is not available"})
            except (ValueError, TypeError):
                self.send(400, {"error": "Invalid request"})

        def do_POST(self):
            nonlocal closing
            if not self.allowed():
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8192:
                    raise ValueError("Request is empty or too large")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError("Expected a JSON object")
                route = urlparse(self.path).path
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
                    elif route == "/api/runs":
                        self.send(201, store.create(body["task"], body["profile"], body["mode"], body["request_key"]))
                    elif route.startswith("/api/runs/") and route.endswith("/cancel") and len(route.split("/")) == 5:
                        self.send(200, store.cancel(route.split("/")[3]))
                    else:
                        self.send(404, {"error": "Unknown route"})
            except (KeyError, TypeError, ValueError) as error:
                self.send(400, {"error": str(error)[:300]})

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    store = RunStore(args.home)
    with worker_lock(store.home, "server.lock") as locked:
        if not locked:
            if args.open:
                webbrowser.open("http://127.0.0.1:" + str(args.port))
            return
        import_history(store)
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
