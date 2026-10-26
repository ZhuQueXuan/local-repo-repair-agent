"""D panel: real HTTP, migration, versions, lifecycle and read-only boundaries."""
import json
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import uuid

from collaboration import CollaborationCoordinator
from collaboration_demo import BASE, ISSUE
from collaboration_mock import ScriptedRunner
from collaboration_panel import (collaboration_view, candidate_view, evidence_view, create_panel_run,
    import_sealed_collaboration, registered_artifact)
from collaboration_protocol import ProtocolError, sha256
from collaboration_store import CollaborationStore, StateConflict
from task_service import make_handler, Supervisor
from task_service_store import ROOT, RunStore, worker_lock


def fixture(store):
    parent = store.create_collaboration("synthetic-arithmetic", "raw", uuid.uuid4().hex, {"feedback_windows": 1},
        input_manifest={"source_prefix": "pkg/", "source_files_sha256": {"pkg/main.py": sha256(BASE)}}, input_issue=ISSUE)
    runner = ScriptedRunner(store, accounting=True)
    CollaborationCoordinator(store, runner).run(parent["id"])
    return parent["id"]


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.home = ROOT / "temp/multi_agent_stage_d_20261023/tests" / uuid.uuid4().hex
        self.store = CollaborationStore(self.home)

    def body(self, **fields):
        return {"task": "split-after-boundary", "profile": "adaptive_history", "mode": "live", "collaboration_mode": "dual_review",
                "policy_version": 1, "request_key": uuid.uuid4().hex, **fields}

    def test_migration_preserves_legacy_payload_and_consistent_backup(self):
        home = self.home / "legacy"
        legacy = RunStore(home)
        old = legacy.create("barrel-sort", "raw", "live", "old")
        store = CollaborationStore(home)
        self.assertEqual(store.get(old["id"]), old)
        backups = list(home.glob("before_collaboration_*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as db:
            self.assertEqual(json.loads(db.execute("SELECT payload FROM runs").fetchone()[0]), old)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        CollaborationStore(home)
        self.assertEqual(len(list(home.glob("before_collaboration_*.sqlite3"))), 1)

    def test_production_capability_does_not_allow_arbitrary_data_directory(self):
        with self.assertRaises(ValueError):
            CollaborationStore(ROOT / "data/forbidden-panel", allow_production=True)

    def test_mode_idempotency_conflicts_and_unsupported_combinations(self):
        body = self.body(request_key="repeated")
        first = create_panel_run(self.store, body)
        self.assertEqual(first["id"], create_panel_run(self.store, body)["id"])
        with self.assertRaises(ValueError):
            create_panel_run(self.store, body | {"collaboration_mode": "single"})
        for fields in ({"mode": "replay"}, {"profile": "raw"}, {"collaboration_mode": "unknown"}, {"policy_version": True}, {"role": "reviewer"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                create_panel_run(self.store, self.body(**fields))

    def test_missing_mode_remains_single_and_queue_cancel_skips_all_dual_roles(self):
        single = create_panel_run(self.store, {"task": "barrel-sort", "profile": "raw", "mode": "live", "request_key": uuid.uuid4().hex})
        self.assertEqual(single["kind"], "live")
        self.assertEqual(collaboration_view(self.store, single["id"])["mode"], "single")
        self.assertEqual(self.store.cancel(single["id"])["status"], "cancelled")
        dual = create_panel_run(self.store, self.body())
        self.store.cancel(dual["id"])
        self.assertEqual([r["status"] for r in self.store.roles(dual["id"])], ["skipped"] * 4)

    def test_version_reports_are_separate_and_cross_run_evidence_rejected(self):
        run_id = fixture(self.store)
        view = collaboration_view(self.store, run_id)
        self.assertEqual(len(view["candidates"]), 2)
        old, new = [candidate_view(self.store, run_id, c["candidate_id"]) for c in view["candidates"]]
        self.assertEqual(old["reports"][0]["status"], "failed")
        self.assertEqual(new["reports"][0]["status"], "passed")
        evidence = view["evidences"][0]
        self.assertIn("content", evidence_view(self.store, run_id, evidence["evidence_id"]))
        other = create_panel_run(self.store, self.body())
        with self.assertRaises(KeyError):
            evidence_view(self.store, other["id"], evidence["evidence_id"])
        with self.assertRaises(ProtocolError):
            candidate_view(self.store, other["id"], old["candidate_id"])

    def test_tampered_snapshot_is_never_previewed(self):
        run_id = fixture(self.store)
        c = collaboration_view(self.store, run_id)["candidates"][0]
        snapshot = self.store.snapshot(run_id, c["candidate_id"])
        doc = snapshot.candidate.documents[0]
        path = snapshot.directory / "objects" / doc.document_id
        path.write_bytes(path.read_bytes() + b"corruption")
        with self.assertRaises(ProtocolError):
            candidate_view(self.store, run_id, c["candidate_id"])

    def test_unknown_usage_and_cost_not_replaced_by_zero(self):
        run_id = fixture(self.store)
        ledger = self.home / "synthetic_ledger.json"
        data = json.loads(ledger.read_text())
        first = next(iter(data["requests"].values()))
        first.update(status="reserved", amount_status="unknown_charge_placeholder")
        first.pop("usage")
        ledger.write_text(json.dumps(data))
        usage = collaboration_view(self.store, run_id)["usage"]
        self.assertIsNone(usage["complete_cost_estimate_cny"])
        self.assertTrue(usage["has_unknown_usage"])
        self.assertEqual(usage["unknown_cost_attempts"], 1)

    def test_restart_preserves_candidate_but_does_not_requeue_roles(self):
        parent = create_panel_run(self.store, self.body())
        self.store.claim_parent(parent["id"])
        meta = self.store.state(parent["id"])["collaboration_state"]
        self.store.claim_role(parent["id"], "repairing", "1" * 64, meta["revision"])
        restarted = CollaborationStore(self.home)
        with worker_lock(self.home) as locked:
            self.assertTrue(locked)
            restarted.reconcile()
        self.assertEqual(restarted.get(parent["id"])["status"], "interrupted")
        self.assertEqual(restarted.roles(parent["id"])[0]["status"], "interrupted")
        with patch("task_service.subprocess.Popen") as process:
            Supervisor(restarted).tick()
        process.assert_not_called()

    def test_supervisor_dispatches_dual_once_and_respects_existing_worker_lock(self):
        parent = create_panel_run(self.store, self.body())
        supervisor = Supervisor(self.store)
        with worker_lock(self.home), patch("task_service.subprocess.Popen") as process:
            supervisor.tick()
            process.assert_not_called()
        with patch("task_service.subprocess.Popen") as process:
            process.return_value.poll.return_value = None
            supervisor.tick()
            supervisor.tick()
            self.assertEqual(process.call_count, 1)
            self.assertIn("--collaboration", process.call_args.args[0])
            self.assertIn(parent["id"], process.call_args.args[0])
        supervisor.log.close()

    def test_sealed_real_history_import_is_read_only_and_idempotent(self):
        import_sealed_collaboration(self.store)
        row = next(r for r in self.store.list() if r["kind"] == "collaboration_history")
        import_sealed_collaboration(self.store)
        self.assertEqual(len(self.store.list()), 1)
        with self.assertRaises(ValueError):
            self.store.cancel(row["id"])
        view = collaboration_view(self.store, row["id"])
        self.assertEqual(len(view["roles"]), 4)
        self.assertEqual([r["phase"] for r in view["roles"]], ["repairing", "reviewing", "revising", "final_review"])
        self.assertEqual(view["review_verdict"], "approve")
        self.assertIn(b"remainder", registered_artifact(self.store, row, "repair.diff"))

    def test_artifacts_require_registration_integrity_and_plain_single_link(self):
        parent = create_panel_run(self.store, self.body())
        root = self.home / "runs" / parent["id"]
        root.mkdir(parents=True)
        path = root / "repair.diff"
        path.write_bytes(b"original diff")
        (root / "artifact_sha256.json").write_text(json.dumps({"repair.diff": sha256(path.read_bytes())}))
        self.store.update(parent["id"], artifacts={"repair.diff": str(path), "private": str(ROOT / ".env")})
        row = self.store.get(parent["id"])
        self.assertEqual(registered_artifact(self.store, row, "repair.diff"), b"original diff")
        with self.assertRaises(ValueError):
            registered_artifact(self.store, row, "private")
        path.write_bytes(b"changed diff")
        with self.assertRaises(ProtocolError):
            registered_artifact(self.store, row, "repair.diff")
        import os
        os.link(path, root / "alias.diff")
        with self.assertRaises(ProtocolError):
            registered_artifact(self.store, row, "repair.diff")


class PanelHttpTests(unittest.TestCase):
    def setUp(self):
        self.home = ROOT / "temp/multi_agent_stage_d_20261023/http" / uuid.uuid4().hex
        self.store = CollaborationStore(self.home)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        with patch("dotenv.dotenv_values", return_value={"BUPT_API_KEY": "http-test-private-key"}):
            self.server.RequestHandlerClass = make_handler(self.store, "panel-test", self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def request(self, route, body=None, headers=None):
        req = urllib.request.Request("http://127.0.0.1:" + str(self.server.server_port) + route,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"X-Task-Token": "panel-test", "Content-Type": "application/json", **(headers or {})})
        return urllib.request.urlopen(req, timeout=15)

    def test_read_only_routes_require_token_host_origin_and_block_private_events(self):
        run_id = fixture(self.store)
        for headers in ({"X-Task-Token": "wrong"}, {"Origin": "http://evil.invalid"}, {"Host": "evil.invalid"}):
            with self.subTest(headers=headers), self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/runs/' + run_id + '/collaboration', headers=headers)
            self.assertEqual(error.exception.code, 403)
        self.store.event(run_id, "test-only", {"text": "http-test-private-key"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/runs/' + run_id + '/events')
        self.assertEqual(error.exception.code, 409)
        self.assertNotIn(b"http-test-private-key", error.exception.read())

    def test_create_conflict_cancel_and_evidence_http_roundtrip(self):
        body = {"task": "split-after-boundary", "profile": "adaptive_history", "mode": "live", "collaboration_mode": "dual_review", "policy_version": 1, "request_key": "http-click"}
        with self.request('/api/runs', body) as response:
            row = json.load(response)
        with self.request('/api/runs', body) as response:
            self.assertEqual(json.load(response)['id'], row['id'])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/runs', body | {"task": "omd-copy"})
        self.assertEqual(error.exception.code, 409)
        with self.request('/api/runs/' + row['id'] + '/cancel', {}) as response:
            self.assertEqual(json.load(response)['status'], 'cancelled')
        run_id = fixture(self.store)
        with self.request('/api/runs/' + run_id + '/collaboration') as response:
            view = json.load(response)
        ref = view['evidences'][0]
        with self.request('/api/runs/' + run_id + '/evidence/' + ref['evidence_id']) as response:
            self.assertEqual(json.load(response)['evidence']['candidate_id'], ref['candidate_id'])


if __name__ == "__main__":
    unittest.main(verbosity=2)
