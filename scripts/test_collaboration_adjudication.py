"""Offline persistence and HTTP checks using frozen, previously billed evidence.

Clones only archived SQLite/snapshots into temp; never opens a production writer
or invokes a model. Candidate code is the trusted public diagnostic fixture.
"""
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
import urllib.error
import urllib.request
import uuid
import zipfile

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("MSWEA_GLOBAL_CONFIG_DIR", str(Path(__file__).resolve().parent.parent / "temp/mini-global"))
os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

from collaboration_adjudication import adjudicate_run, read_batches, read_proof
from collaboration_panel import candidate_view, collaboration_view
from collaboration_protocol import ProtocolError, canonical_bytes, sha256
from collaboration_store import CollaborationStore
from task_service_store import ROOT, RunStore

RUN = "d3173d4153e544ccbf0eb066834848f8"
ARCHIVE = ROOT / "experiments/multi_agent_response_recheck_live_20261024"


def fresh_archive():
    home = ROOT / "temp/adjudication_panel_tests" / uuid.uuid4().hex
    home.mkdir(parents=True)
    with sqlite3.connect(ARCHIVE / "production-after.sqlite3") as source, sqlite3.connect(home / "runs.sqlite3") as target:
        source.backup(target)
        snapshots = [json.loads(r[0]) for r in source.execute(
            "SELECT payload FROM collaboration_objects WHERE run_id=? AND kind='snapshot'", (RUN,))]
    names = {}
    for name in ("snapshot-v1.zip", "snapshot-v2.zip"):
        with zipfile.ZipFile(ARCHIVE / name) as archive:
            names[json.loads(archive.read("candidate.json"))["candidate_id"]] = name
    for value in snapshots:
        name = names[value["candidate"]["candidate_id"]]
        directory = home / value["relative_directory"]
        directory.mkdir(parents=True)
        with zipfile.ZipFile(ARCHIVE / name) as archive:
            for entry in archive.namelist():
                target = directory / entry
                if not target.resolve().is_relative_to(directory.resolve()):
                    raise ValueError("Unsafe fixture archive path")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(entry))
    return home


def parents(store):
    with store.connect() as db:
        return db.execute("SELECT id,payload FROM runs ORDER BY id").fetchall()


class PersistentAdjudicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = CollaborationStore(fresh_archive())
        cls.original_parents = [tuple(v) for v in parents(cls.original)]
        cls.batch = adjudicate_run(cls.original, RUN)

    def setUp(self):
        self.home = ROOT / "temp/adjudication_panel_tests" / uuid.uuid4().hex
        self.home.mkdir(parents=True)
        with self.original.connect() as source, sqlite3.connect(self.home / "runs.sqlite3") as target:
            source.backup(target)
        shutil.copytree(self.original.home / "snapshots", self.home / "snapshots")
        shutil.copytree(self.original.home / "adjudications", self.home / "adjudications")
        self.store = CollaborationStore(self.home)

    def test_real_archived_versions_and_scope_not_model_votes(self):
        decisions = self.batch["decisions"]
        self.assertEqual(len(decisions), 4)
        confirmed = [d for d in decisions if d["verdict"] == "confirmed"]
        self.assertEqual(len(confirmed), 1)
        d = confirmed[0]
        self.assertEqual(d["finding_id"], "f1-falsey-remainder-dropped")
        self.assertEqual(d["resolution"], "resolved_on_checked_cases")
        self.assertEqual(d["impact"], "improved_on_checked_cases")
        self.assertEqual(set(d["improved_checks"]), {"zero", "none", "false", "zeros"})
        self.assertEqual(d["prior_visibility"], "already_reported")
        self.assertEqual(sum(s["summary"]["new_confirmed_groups_within_scope"] for s in self.batch["summaries"]), 0)
        self.assertTrue(all(d["verdict"] == "unresolved" for d in decisions if d not in confirmed))
        counts = sorted(sum(v == "passed" for v in r["results"].values()) for r in self.batch["receipts"])
        self.assertEqual(counts, [2, 6])

    def test_restart_and_duplicate_do_not_reexecute_or_rewrite_outcomes(self):
        with patch("review_adjudication.subprocess.Popen") as launch:
            restored = adjudicate_run(CollaborationStore(self.home), RUN)
        launch.assert_not_called()
        self.assertEqual(restored, self.batch)
        self.assertEqual([tuple(v) for v in parents(self.store)], self.original_parents)
        self.assertEqual(self.store.get(RUN)["repair_result"], "passed")
        self.assertEqual(len(read_batches(self.store, RUN)), 1)

    def test_additive_migration_has_backup_and_read_only_projection_no_migration(self):
        home = fresh_archive()
        old = RunStore(home)
        before = [tuple(v) for v in parents(old)]
        self.assertEqual(read_batches(old, RUN), [])
        with old.connect() as db:
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='collaboration_adjudications'").fetchone())
        store = CollaborationStore(home)
        self.assertEqual([tuple(v) for v in parents(store)], before)
        backups = list(home.glob("before_adjudication_*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 45)
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='collaboration_adjudications'").fetchone())
        CollaborationStore(home)
        self.assertEqual(list(home.glob("before_adjudication_*.sqlite3")), backups)

    def test_original_review_and_new_version_have_separate_decisions(self):
        value = collaboration_view(self.store, RUN)
        for candidate in value["candidates"]:
            view = candidate_view(self.store, RUN, candidate["candidate_id"])
            self.assertEqual(len(view["adjudications"]), 2)
            self.assertTrue(all(d["reviewed_candidate_id"] == candidate["candidate_id"] for d in view["adjudications"]))
            if view["is_final"]:
                self.assertTrue(all(d["verdict"] == "unresolved" for d in view["adjudications"]))
            else:
                self.assertEqual(sum(d["verdict"] == "confirmed" for d in view["adjudications"]), 1)

    def test_unregistered_new_claims_remain_unknown_even_with_pass_fail_checks(self):
        store = CollaborationStore(fresh_archive())
        with patch("collaboration_adjudication._registry", return_value={"schema_version": 1, "scope": "No trusted exact mapping", "bindings": []}):
            batch = adjudicate_run(store, RUN)
        self.assertEqual(len(batch["decisions"]), 4)
        self.assertTrue(all(d["verdict"] == "unresolved" and not d["check_ids"] for d in batch["decisions"]))

    def test_result_and_sql_row_tamper_fail_closed(self):
        directory = self.home / "adjudications" / self.batch["batch_id"]
        original = (directory / "result.json").read_bytes()
        (directory / "result.json").write_bytes(original + b" ")
        with self.assertRaisesRegex(ProtocolError, "result_changed"):
            read_batches(self.store, RUN)
        (directory / "result.json").write_bytes(original)
        with self.store.transaction() as db:
            db.execute("UPDATE collaboration_adjudications SET payload=payload||' '")
        with self.assertRaisesRegex(ProtocolError, "row_changed"):
            read_batches(self.store, RUN)

    def test_proof_and_its_manifest_cannot_be_replaced_together(self):
        directory = self.home / "adjudications" / self.batch["batch_id"]
        (directory / "proof.zip").write_bytes(b"untrusted replacement")
        with self.assertRaisesRegex(ProtocolError, "proof_hash_mismatch"):
            read_proof(self.store, RUN, self.batch["batch_id"], "proof.zip")
        manifest = json.loads((directory / "artifact_sha256.json").read_bytes())
        manifest["proof.zip"] = sha256(b"untrusted replacement")
        (directory / "artifact_sha256.json").write_bytes(canonical_bytes(manifest))
        with self.assertRaisesRegex(ProtocolError, "manifest_changed"):
            read_proof(self.store, RUN, self.batch["batch_id"], "proof.zip")

    def test_registered_proof_has_complete_receipt_bytes_and_cross_run_is_denied(self):
        batch_id = self.batch["batch_id"]
        raw = read_proof(self.store, RUN, batch_id, "proof.zip")
        hashes = json.loads(read_proof(self.store, RUN, batch_id, "proof-files.json"))
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertEqual(set(hashes), set(archive.namelist()))
            self.assertTrue(all(sha256(archive.read(p)) == h for p, h in hashes.items()))
        for name in ("../.env", ".env", "unknown.json"):
            with self.subTest(name=name), self.assertRaises(ProtocolError):
                read_proof(self.store, RUN, batch_id, name)
        other = next(row[0] for row in parents(self.store) if row[0] != RUN)
        with self.assertRaisesRegex(ProtocolError, "not_for_run"):
            read_proof(self.store, other, batch_id, "result.json")

    def test_untrusted_candidate_modification_blocks_old_decision_projection(self):
        d = self.batch["decisions"][0]
        snapshot = self.store.snapshot(RUN, d["reviewed_candidate_id"])
        path = snapshot.directory / "objects" / snapshot.candidate.documents[0].document_id
        path.write_bytes(path.read_bytes() + b"later mutation")
        with self.assertRaises(ProtocolError):
            read_batches(self.store, RUN)

    def test_worker_checks_only_after_runner_close_and_failure_preserves_outcome(self):
        from collaboration_live import run_live_worker
        for fail in (False, True):
            with self.subTest(supplemental_failure=fail):
                run_id = "offline-hook-" + uuid.uuid4().hex
                row = {"id": run_id, "status": "queued"}
                events = []
                fake_store = SimpleNamespace(get=lambda _: row.copy())
                runner = Mock()
                runner.setup.side_effect = lambda: events.append("setup")
                runner.finalize.side_effect = lambda _: events.append("finalize")
                runner.close.side_effect = lambda: events.append("close")
                def finish(_):
                    events.append("coordinator")
                    row.update(status="finished", repair_result="passed", review_verdict="approve")
                    return row.copy()
                def supplement(store, current_id):
                    self.assertEqual((store, current_id), (fake_store, run_id))
                    self.assertEqual(row["status"], "finished")
                    self.assertEqual(events[-1], "close")
                    events.append("adjudication")
                    if fail:
                        raise ProtocolError("Unavailable offline check")
                coordinator = Mock()
                coordinator.run.side_effect = finish
                with patch("collaboration_live.LiveRunner", return_value=runner), patch("collaboration_live.CollaborationCoordinator", return_value=coordinator), patch("collaboration_adjudication.adjudicate_run", side_effect=supplement):
                    result = run_live_worker(fake_store, run_id)
                self.assertEqual(events, ["setup", "coordinator", "finalize", "close", "adjudication"])
                self.assertEqual(result, row)
                self.assertEqual(result["repair_result"], "passed")
                if fail:
                    error = json.loads((ROOT / "temp/task_service" / run_id / "adjudication-error.json").read_bytes())
                    self.assertEqual(error["type"], "ProtocolError")

    def test_http_read_only_auth_scope_and_integrity(self):
        from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
        from task_service import make_handler
        server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        server.RequestHandlerClass = make_handler(self.store, "offline-token", server.server_port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        prefix = "/api/runs/" + RUN + "/adjudications/" + self.batch["batch_id"] + "/"
        def request(path, token="offline-token", method="GET"):
            req = urllib.request.Request("http://127.0.0.1:" + str(server.server_port) + path,
                headers={"X-Task-Token": token, "Content-Type": "application/json"}, method=method,
                data=b"{}" if method == "POST" else None)
            return urllib.request.urlopen(req, timeout=10)
        try:
            with patch("review_adjudication.subprocess.Popen") as launch:
                with request(prefix + "result.json") as response:
                    self.assertEqual(json.load(response)["run_id"], RUN)
            launch.assert_not_called()
            for path, token, method, expected in ((prefix + "result.json", "wrong", "GET", 403),
                    (prefix + "result.json", "offline-token", "POST", 404),
                    (prefix + "%2e%2e%2f.env", "offline-token", "GET", 400)):
                with self.subTest(path=path, method=method), self.assertRaises(urllib.error.HTTPError) as error:
                    request(path, token, method)
                self.assertEqual(error.exception.code, expected)
            self.assertEqual([tuple(v) for v in parents(self.store)], self.original_parents)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
