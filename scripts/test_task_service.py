"""Meaningful lifecycle, cancellation, provenance and loopback API checks; no API fees."""
import hashlib
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import uuid

from task_service_store import ROOT, RunStore, history_record, import_history, worker_lock
from task_service import make_handler
from task_service_worker import RunBudget, ServiceModelHooks, ServiceEnvironmentHooks, ServiceRawEnvironment, collect_patch, check_cancel, run_one
from task_workspace import prepare_task
from minisweagent.exceptions import InterruptAgentFlow


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = RunStore(ROOT / "temp/task_service_tests" / uuid.uuid4().hex)

    def create(self, key=None):
        return self.store.create("barrel-sort", "adaptive_history", "live", key or uuid.uuid4().hex)

    def test_idempotent_creation_and_mismatched_reuse(self):
        first = self.create("same-click")
        self.assertEqual(first["id"], self.create("same-click")["id"])
        with self.assertRaises(ValueError):
            self.store.create("omd-copy", "adaptive_history", "live", "same-click")
        self.assertEqual(len(self.store.list()), 1)

    def test_cancel_queued_never_claims(self):
        row = self.create()
        self.assertEqual(self.store.cancel(row["id"])["status"], "cancelled")
        self.assertIsNone(self.store.update(row["id"], expected={"queued"}, status="running"))
        self.assertNotIn("model_request", [e["kind"] for e in self.store.events(row["id"])])

    def test_cancel_claimed_is_a_request_until_worker_ends(self):
        row = self.create()
        self.store.update(row["id"], status="running")
        self.assertEqual(self.store.cancel(row["id"])["status"], "cancel_requested")
        self.store.update(row["id"], status="finished", repair_result="passed")
        self.assertEqual(self.store.get(row["id"])["status"], "cancelled")
        self.assertEqual(self.store.get(row["id"])["repair_result"], "passed")

    def test_restart_marks_interrupted_and_does_not_requeue(self):
        row = self.create()
        self.store.update(row["id"], status="running", agent_exit=None)
        restarted = RunStore(self.store.home)
        restarted.reconcile()
        self.assertEqual(restarted.get(row["id"])["status"], "interrupted")
        self.assertFalse(any(r["status"] == "queued" for r in restarted.list()))

    def test_lock_rejects_second_owner_and_releases(self):
        with worker_lock(self.store.home) as first:
            self.assertTrue(first)
            with worker_lock(self.store.home) as second:
                self.assertFalse(second)
        with worker_lock(self.store.home) as third:
            self.assertTrue(third)

    def test_terminal_runs_and_history_are_not_reexecuted(self):
        row = self.create()
        self.store.update(row["id"], status="finished", repair_result="failed")
        self.assertIsNone(self.store.update(row["id"], status="queued"))
        import_history(self.store)
        history = next(r for r in self.store.list() if r["kind"] == "history")
        with self.assertRaises(ValueError):
            self.store.cancel(history["id"])

    def test_historical_correct_patch_can_have_failed_agent_flow(self):
        path = ROOT / "experiments/retrieval_delivery_trial_20261015/barrel-sort/repository_retrieval/summary.json"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        row = history_record(path)
        self.assertEqual(row["agent_exit"], "LimitsExceeded")
        self.assertEqual(row["repair_result"], "passed")
        self.assertIsNone(row["metrics"]["complete_cost_estimate_cny"])
        self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_missing_historical_fields_stay_unknown(self):
        # Fixture lives inside project temp, but history paths use project-relative IDs.
        path = self.store.home / "summary.json"
        path.write_text(json.dumps({"task_id": "legacy", "exit_status": "Unknown"}), encoding="utf-8")
        row = history_record(path)
        self.assertEqual(row["repair_result"], "unknown")
        self.assertIsNone(row["metrics"]["requests"])
        self.assertIsNone(row["metrics"]["prompt_tokens"])

    def test_fixed_task_and_strategy_allowlists(self):
        for task, profile, mode in [("../", "raw", "live"), ("barrel-sort", "arbitrary", "live"), ("omd-copy", "raw", "replay"), ("windowed-empty", "raw", "live")]:
            with self.subTest(task=task, profile=profile, mode=mode), self.assertRaises(ValueError):
                self.store.create(task, profile, mode, uuid.uuid4().hex)

    def test_events_have_persistent_order_and_actor(self):
        row = self.create()
        self.store.event(row["id"], "handoff", {"evidence": "path:line"}, actor="repairer")
        events = RunStore(self.store.home).events(row["id"])
        self.assertEqual(events[-1]["actor"], "repairer")
        self.assertGreater(events[-1]["seq"], events[0]["seq"])
        self.assertEqual(len(self.store.events(row["id"], events[0]["seq"])), 1)

    def test_preparation_error_is_persisted_without_model_requests(self):
        row = self.create()
        (ROOT / "temp/task_service" / row["id"]).mkdir(parents=True)
        with patch("task_service_worker.freeze_inputs", return_value={}), patch("task_service_worker.prepare_task", side_effect=RuntimeError("offline preparation failure")):
            run_one(self.store, row["id"])
        ended = self.store.get(row["id"])
        self.assertEqual(ended["status"], "failed")
        self.assertEqual(ended["metrics"]["requests"], 0)
        self.assertTrue(Path(ended["artifacts"]["summary.json"]).is_file())

    def test_collector_recovery_does_not_fabricate_exact_elapsed_time(self):
        path = ROOT / "experiments/repository_retrieval_dev_20261014/repository_retrieval/summary.json"
        row = history_record(path)
        self.assertIn("离线恢复", row["reason"])
        self.assertIsNone(row["metrics"]["elapsed_agent_seconds"])

    def test_grader_timeout_leaves_terminal_record_and_no_model_spend(self):
        row = self.create()
        work = self.store.home / "checkout"
        work.mkdir()
        with patch("task_service_worker.freeze_inputs", return_value={}), patch("task_service_worker.prepare_task", return_value=(work, {"task_id": "fixture"})), patch("task_service_worker.evaluate", side_effect=TimeoutError("offline grading timeout")), patch("task_service_worker.collect_patch", return_value=[]):
            run_one(self.store, row["id"])
        record = self.store.get(row["id"])
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["repair_result"], "unknown")
        self.assertEqual(record["metrics"]["requests"], 0)

    def test_patch_collection_failure_preserves_record_without_reexecuting_model(self):
        row = self.store.create("barrel-sort", "retrieval_on_change", "replay", uuid.uuid4().hex)
        work = self.store.home / "checkout"
        work.mkdir()
        baseline = {"issue_checks": {"successful": False}, "upstream_targeted": {"successful": True}}
        with patch("task_service_worker.freeze_inputs", return_value={}), patch("task_service_worker.prepare_task", return_value=(work, {"task_id": "fixture"})), patch("task_service_worker.evaluate", return_value=baseline), patch("task_service_worker.subprocess.run"), patch("task_service_worker.shutil.copytree"), patch("task_service_worker.collect_patch", side_effect=RuntimeError("offline collector failure")):
            run_one(self.store, row["id"])
        record = self.store.get(row["id"])
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["metrics"]["requests"], 0)
        self.assertIn("patch_collection_failed", [event["kind"] for event in self.store.events(row["id"])])

    def test_real_shell_timeout_returns_a_recorded_failure(self):
        row = self.create()
        self.store.update(row["id"], status="running")
        env = ServiceRawEnvironment(self.store.home, Path(r"D:\Git\bin\bash.exe"), service_store=self.store, service_run_id=row["id"])
        result = env.execute({"command": "python -B -c 'import time; time.sleep(5)'"}, timeout=0.15)
        self.assertEqual(result["returncode"], -1)
        self.assertIn("timed out", result["exception_info"])
        self.assertEqual(self.store.events(row["id"])[-1]["kind"], "action_finished")

    def test_new_implementation_file_is_explicitly_rejected(self):
        work, manifest = prepare_task("barrel-sort")
        (work / "boltons/new_service_test_file.py").write_text("value = 1\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "existing implementation files"):
            collect_patch("barrel-sort", work, self.store.home, manifest)

    def test_cancel_after_billed_response_prevents_next_action(self):
        row = self.create()
        store = self.store
        store.update(row["id"], status="running")

        class Base:
            def __init__(self):
                self.requests, self.billed = 0, False

            def query(self, messages):
                self.requests += 1
                self.billed = True
                store.cancel(row["id"])
                return {"command": "must not execute"}

        class Fake(ServiceModelHooks, Base):
            pass

        model = Fake(service_store=store, service_run_id=row["id"])
        with patch("task_service_worker.model_metrics", return_value={"requests": 1}), self.assertRaises(InterruptAgentFlow):
            model.query([])
        self.assertTrue(model.billed)
        self.assertEqual(store.get(row["id"])["metrics"]["requests"], 1)

    def test_cancel_also_blocks_a_new_fallback_reservation(self):
        row = self.create()
        self.store.update(row["id"], status="running")
        self.store.cancel(row["id"])
        budget = RunBudget(self.store, row["id"])
        with patch("task_service_worker.SharedBudget.reserve") as reserve, self.assertRaises(InterruptAgentFlow):
            budget.reserve(provider="deepseek_official")
        reserve.assert_not_called()

    def test_cancel_before_shell_execute_blocks_the_shell(self):
        row = self.create()
        self.store.cancel(row["id"])

        class Base:
            calls = 0

            def __init__(self):
                pass

            def execute(self, *args, **kwargs):
                self.calls += 1

        class Fake(ServiceEnvironmentHooks, Base):
            pass

        env = Fake(service_store=self.store, service_run_id=row["id"])
        with self.assertRaises(InterruptAgentFlow):
            env.execute({"command": "echo must not execute"})
        self.assertEqual(env.calls, 0)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = RunStore(ROOT / "temp/task_service_api_tests" / uuid.uuid4().hex)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        cls.port = cls.server.server_port
        cls.server.RequestHandlerClass = make_handler(cls.store, "offline-test-token", cls.port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)

    def request(self, path, body=None, headers=None):
        request = urllib.request.Request("http://127.0.0.1:" + str(self.port) + path,
                  data=json.dumps(body).encode() if body is not None else None,
                  headers={"X-Task-Token": "offline-test-token", "Content-Type": "application/json", **(headers or {})})
        return urllib.request.urlopen(request, timeout=5)

    def test_api_requires_local_token_and_origin(self):
        for headers in [{"X-Task-Token": "wrong"}, {"Origin": "http://evil.invalid"}, {"Host": "evil.invalid"}]:
            with self.subTest(headers=headers), self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/runs", headers=headers)
            self.assertEqual(error.exception.code, 403)

    def test_create_cancel_query_roundtrip(self):
        with self.request("/api/runs", {"task": "barrel-sort", "profile": "raw", "mode": "live", "request_key": uuid.uuid4().hex}) as response:
            row = json.load(response)
        with self.request("/api/runs/" + row["id"] + "/cancel", {}) as response:
            self.assertEqual(json.load(response)["status"], "cancelled")
        with self.request("/api/runs/" + row["id"] + "/events") as response:
            self.assertEqual(json.load(response)[-1]["kind"], "cancelled_before_start")

    def test_artifact_route_does_not_read_private_or_arbitrary_files(self):
        row = self.store.create("barrel-sort", "raw", "live", uuid.uuid4().hex)
        self.store.update(row["id"], artifacts={"blocked": str(ROOT / ".env")})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/runs/" + row["id"] + "/artifacts/blocked")
        self.assertEqual(error.exception.code, 400)

    def test_full_download_preserves_bytes_and_rejects_tampering_and_credentials(self):
        row = self.store.create("barrel-sort", "raw", "live", uuid.uuid4().hex)
        root = self.store.home / "runs" / row["id"]
        root.mkdir(parents=True)
        raw = ("完整补丁\r\n" * 30000).encode("utf-8")
        path = root / "repair.diff"
        path.write_bytes(raw)
        manifest = root / "artifact_sha256.json"
        manifest.write_text(json.dumps({"repair.diff": hashlib.sha256(raw).hexdigest()}), encoding="utf-8")
        self.store.update(row["id"], artifacts={"repair.diff": str(path)})
        route = "/api/runs/" + row["id"] + "/artifacts/repair.diff"
        with self.request(route) as response:
            self.assertIn(b"preview truncated", response.read())
        with self.request(route + "?download=1") as response:
            self.assertEqual(response.read(), raw)
            self.assertEqual(response.headers["X-Content-SHA256"], hashlib.sha256(raw).hexdigest())
            self.assertIn("attachment", response.headers["Content-Disposition"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(route + "?download=1", headers={"X-Task-Token": "wrong"})
        self.assertEqual(error.exception.code, 403)
        path.write_bytes(raw + b"tampered")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(route + "?download=1")
        self.assertEqual(error.exception.code, 400)
        secret = "fixture-only-credential-20261030"
        path.write_text(secret, encoding="utf-8")
        manifest.write_text(json.dumps({"repair.diff": hashlib.sha256(secret.encode()).hexdigest()}), encoding="utf-8")
        original = self.server.RequestHandlerClass
        try:
            with patch("dotenv.dotenv_values", return_value={"FIXTURE_KEY": secret}):
                self.server.RequestHandlerClass = make_handler(self.store, "offline-test-token", self.port)
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(route + "?download=1")
            self.assertEqual(error.exception.code, 409)
            self.assertIsNone(error.exception.headers.get("Content-Disposition"))
            self.assertNotIn(secret.encode(), error.exception.read())
        finally:
            self.server.RequestHandlerClass = original

    def test_z_shutdown_refuses_pending_work_and_can_stop_when_idle(self):
        row = self.store.create("barrel-sort", "raw", "live", uuid.uuid4().hex)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/shutdown", {})
        self.assertEqual(error.exception.code, 409)
        for pending in self.store.list():
            self.store.cancel(pending["id"])
        stopped = threading.Event()
        with patch.object(self.server, "shutdown", side_effect=stopped.set):
            with self.request("/api/shutdown", {}) as response:
                self.assertEqual(json.load(response)["status"], "stopping")
            self.assertTrue(stopped.wait(2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
