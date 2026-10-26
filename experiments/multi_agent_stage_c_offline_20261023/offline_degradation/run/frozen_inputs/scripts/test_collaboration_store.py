"""Durable stage-B identity, migration, provenance, cancellation and accounting."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
import uuid

from collaboration import RoleBudget, attributed_usage, role_identity
from collaboration_mock import ScriptedRunner
from collaboration_demo import BASE, ISSUE
from collaboration_protocol import ProtocolError, canonical_bytes, sha256
from collaboration_store import CollaborationStore, CollaborationStopped, StateConflict
from deepseek_budget import SharedBudget, load_budget_config
from task_service_store import ROOT, RunStore, worker_lock


def make_store(extra_policy=None):
    store = CollaborationStore(ROOT / "temp/multi_agent_stage_b_20261022/tests" / uuid.uuid4().hex)
    parent = store.create_collaboration("synthetic-arithmetic", "raw", "request", {"version": 1, "feedback_windows": 1, **(extra_policy or {})},
        input_manifest={"source_prefix": "pkg/", "source_files_sha256": {"pkg/main.py": sha256(BASE)}}, input_issue=ISSUE)
    return store, parent


def claim_first(store, parent):
    store.claim_parent(parent["id"])
    return store.claim_role(parent["id"], "repairing", sha256(b"input"), 0)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store, self.parent = make_store()

    def test_stage_b_refuses_production_database(self):
        with self.assertRaisesRegex(ValueError, "temp-only"):
            CollaborationStore(ROOT / "data/task_service")

    def test_old_database_migration_preserves_rows_and_saves_consistent_backup(self):
        home = self.store.home / "old"
        old = RunStore(home)
        row = old.create("barrel-sort", "raw", "live", "old-key")
        with old.connect() as db:
            before = db.execute("SELECT payload FROM runs WHERE id=?", (row["id"],)).fetchone()[0]
        migrated = CollaborationStore(home)
        with migrated.connect() as db:
            self.assertEqual(before, db.execute("SELECT payload FROM runs WHERE id=?", (row["id"],)).fetchone()[0])
        backups = list(home.glob("before_collaboration_*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as db:
            self.assertEqual(before, db.execute("SELECT payload FROM runs WHERE id=?", (row["id"],)).fetchone()[0])
        CollaborationStore(home)
        self.assertEqual(len(list(home.glob("before_collaboration_*.sqlite3"))), 1)

    def test_unsupported_schema_is_not_rebuilt(self):
        with self.store.connect() as db:
            db.execute("UPDATE collaboration_schema SET version=99")
        with self.assertRaises(StateConflict):
            CollaborationStore(self.store.home)
        self.assertEqual(self.store.get(self.parent["id"])["status"], "queued")

    def test_migration_lock_prevents_competing_schema_writer(self):
        with worker_lock(self.store.home, "collaboration_migration.lock") as acquired:
            self.assertTrue(acquired)
            with self.assertRaisesRegex(StateConflict, "migration_already_owned"):
                CollaborationStore(self.store.home)
        self.assertEqual(CollaborationStore(self.store.home).get(self.parent["id"])["status"], "queued")

    def test_parent_idempotency_includes_policy_mode_task_and_profile(self):
        same = self.store.create_collaboration("synthetic-arithmetic", "raw", "request", {"feedback_windows": 1, "version": 1},
            input_manifest={"source_prefix": "pkg/", "source_files_sha256": {"pkg/main.py": sha256(BASE)}}, input_issue=ISSUE)
        self.assertEqual(same["id"], self.parent["id"])
        for task, profile, policy in [("barrel-sort", "raw", {"version": 1}), ("split-after-boundary", "adaptive_history", {"version": 1}), ("split-after-boundary", "raw", {"version": 2})]:
            with self.subTest(task=task, profile=profile), self.assertRaises(StateConflict):
                self.store.create_collaboration(task, profile, "request", policy)
        self.assertEqual(len(self.store.list()), 1)

    def test_first_candidate_must_match_frozen_parent_inputs(self):
        from collaboration import RoleContext
        from review_tools import seal_candidate
        from test_collaboration_protocol import StageAFixture
        fixture = StageAFixture(run_id=self.parent["id"])
        role = claim_first(self.store, self.parent)
        snapshot = seal_candidate(fixture.manifest, fixture.baseline, fixture.work, fixture.issue, fixture.root / "wrong-input",
                                  run_id=self.parent["id"], producer_role_run_id=role["id"])
        with self.assertRaisesRegex(ProtocolError, "parent_input_mismatch"):
            self.store.register_snapshot(role["id"], snapshot)

    def test_same_request_key_cannot_change_synthetic_input(self):
        with self.assertRaises(StateConflict):
            self.store.create_collaboration("synthetic-arithmetic", "raw", "request", {"version": 1, "feedback_windows": 1},
                input_manifest={"source_prefix": "pkg/", "source_files_sha256": {"pkg/main.py": sha256(BASE)}}, input_issue=ISSUE + b"Changed requirements")

    def test_shared_repair_session_and_distinct_review_sessions_persist(self):
        roles = self.store.roles(self.parent["id"])
        self.assertEqual(roles[0]["session_id"], roles[2]["session_id"])
        self.assertNotEqual(roles[1]["session_id"], roles[3]["session_id"])
        self.assertEqual(roles, CollaborationStore(self.store.home).roles(self.parent["id"]))

    def test_duplicate_concurrent_claim_has_one_winner(self):
        self.store.claim_parent(self.parent["id"])
        def claim():
            try:
                self.store.claim_role(self.parent["id"], "repairing", sha256(b"input"), 0)
                return "won"
            except StateConflict:
                return "lost"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(sorted(outcomes), ["lost", "won"])
        self.assertEqual(sum(r["status"] == "running" for r in self.store.roles(self.parent["id"])), 1)

    def test_wrong_phase_stale_revision_and_cancelled_claim_are_rejected(self):
        role = claim_first(self.store, self.parent)
        with self.assertRaises(StateConflict):
            self.store.claim_role(self.parent["id"], "reviewing", sha256(b"input"), 1)
        self.store.cancel(self.parent["id"])
        with self.assertRaises(CollaborationStopped):
            self.store.complete_role(role["id"], {}, 1)

    def test_queued_cancellation_skips_all_roles_and_cannot_be_claimed(self):
        self.store.cancel(self.parent["id"])
        self.assertFalse(self.store.claim_parent(self.parent["id"]))
        self.assertTrue(all(r["status"] == "skipped" for r in self.store.roles(self.parent["id"])))

    def test_role_cannot_finish_without_registered_candidate(self):
        role = claim_first(self.store, self.parent)
        with self.assertRaises(ProtocolError):
            self.store.complete_role(role["id"], {"candidate_id": "invented"}, 1)
        self.assertEqual(self.store.role(role["id"])["status"], "running")

    def test_prepared_request_blocks_role_completion(self):
        role = claim_first(self.store, self.parent)
        self.store.prepare_request(role["id"], "q", sha256(b"q"))
        with self.assertRaisesRegex(StateConflict, "unsettled"):
            self.store.complete_role(role["id"], {}, 1)

    def test_logical_request_idempotency_checks_input_and_terminal_state(self):
        role = claim_first(self.store, self.parent)
        first = self.store.prepare_request(role["id"], "q", sha256(b"q"))
        self.assertEqual(first["id"], self.store.prepare_request(role["id"], "q", sha256(b"q"))["id"])
        with self.assertRaises(StateConflict):
            self.store.prepare_request(role["id"], "q", sha256(b"different"))
        self.store.stop(self.parent["id"], "interrupted", "injected crash")
        self.assertEqual(self.store.request(first["id"])["state"], "outcome_unknown")
        role_after = self.store.role(role["id"])
        self.assertIsNone(role_after["ended"])
        self.assertIn("interruption_detected", role_after)


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.store, self.parent = make_store()
        self.role = claim_first(self.store, self.parent)
        self.request = self.store.prepare_request(self.role["id"], "q", sha256(b"q"))
        self.path = self.store.home / "synthetic-ledger.json"
        self.seed = {"historical_estimated_cost_cny": "9.50", "historical_unknown_charge_reservation_cny": "0.20", "blocked": False,
                     "requests": {"legacy": {"status": "settled", "amount_cny": "0.01"}}}
        self.path.write_bytes(canonical_bytes(self.seed))
        self.config = dict(load_budget_config(), max_total_cost_cny=None)
        self.budget = RoleBudget(self.config, self.store, role_identity(self.role), self.request["id"], ledger_path=self.path, temp_dir=self.store.home / "budget-temp")

    def receipt(self, provider="first", price="0.01", unpriced=False):
        ticket = self.budget.reserve(provider=provider)
        self.budget.before_dispatch(ticket)
        if unpriced:
            self.budget.record_unpriced(ticket, {"prompt_tokens": 10, "completion_tokens": 2})
        else:
            self.budget.settle(ticket, price, usage={"prompt_tokens": 10, "completion_tokens": 2})
        self.budget.receipt_recorded(ticket)
        return ticket

    def test_attribution_is_atomic_with_ticket_and_does_not_change_legacy_entries(self):
        ticket = self.budget.reserve(provider="first")
        ledger = json.loads(self.path.read_text())
        self.assertEqual(ledger["requests"][ticket]["attribution"]["run_id"], self.parent["id"])
        self.assertEqual(ledger["requests"][ticket]["attribution"]["role_run_id"], self.role["id"])
        self.assertEqual(ledger["requests"]["legacy"], self.seed["requests"]["legacy"])
        self.assertEqual(ledger["historical_estimated_cost_cny"], "9.50")

    def test_crash_between_reserve_and_event_keeps_owner_and_prevents_resend(self):
        with patch.object(self.store, "request_event", side_effect=OSError("injected database write failure")):
            with self.assertRaises(OSError):
                self.budget.reserve(provider="first")
        usage = attributed_usage(self.budget, self.parent["id"])
        self.assertEqual(usage["attempts"], 1)
        self.assertTrue(usage["has_unknown_usage"])
        self.assertIsNone(usage["complete_cost_estimate_cny"])
        with self.assertRaises(StateConflict):
            self.budget.reserve(provider="first")
        self.assertEqual(attributed_usage(self.budget, self.parent["id"])["attempts"], 1)

    def test_duplicate_dispatch_is_rejected_before_another_transport(self):
        ticket = self.budget.reserve(provider="first")
        self.budget.before_dispatch(ticket)
        with self.assertRaises(StateConflict):
            self.budget.before_dispatch(ticket)

    def test_cancel_before_reserve_or_dispatch_prevents_new_work(self):
        ticket = self.budget.reserve(provider="first")
        self.store.cancel(self.parent["id"])
        with self.assertRaises(CollaborationStopped):
            self.budget.before_dispatch(ticket)
        with self.assertRaises(CollaborationStopped):
            self.budget.reserve(provider="fallback")

    def test_billed_response_after_cancel_stays_accounted(self):
        ticket = self.budget.reserve(provider="first")
        self.budget.before_dispatch(ticket)
        self.store.cancel(self.parent["id"])
        self.budget.settle(ticket, "0.01", usage={"prompt_tokens": 11, "completion_tokens": 3})
        with self.assertRaises(CollaborationStopped):
            self.budget.receipt_recorded(ticket)
        usage = attributed_usage(self.budget, self.parent["id"])
        self.assertEqual(usage["known_prompt_tokens"], 11)
        self.assertEqual(usage["known_price_estimate_cny"], 0.01)

    def test_unpriced_receipt_keeps_complete_cost_unknown(self):
        self.receipt(unpriced=True)
        usage = attributed_usage(self.budget, self.parent["id"])
        self.assertFalse(usage["has_unknown_usage"])
        self.assertEqual(usage["known_prompt_tokens"], 10)
        self.assertEqual(usage["unknown_cost_attempts"], 1)
        self.assertIsNone(usage["complete_cost_estimate_cny"])

    def test_failed_first_and_successful_fallback_count_both_tickets(self):
        first = self.budget.reserve(provider="first")
        self.budget.before_dispatch(first)
        self.budget.note_failure(first, {"kind": "connection_error", "failover_eligible": True})
        self.receipt(provider="fallback")
        usage = attributed_usage(self.budget, self.parent["id"])
        self.assertEqual(usage["attempts"], 2)
        self.assertEqual(usage["logical_requests"], 1)
        self.assertEqual(usage["unknown_cost_attempts"], 1)
        self.assertIsNone(usage["complete_cost_estimate_cny"])

    def test_second_provider_not_allowed_before_first_outcome(self):
        self.budget.reserve(provider="first")
        with self.assertRaises(StateConflict):
            self.budget.reserve(provider="fallback")

    def test_invalid_attribution_and_usage_are_rejected_before_ledger_write(self):
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.budget._attribution({"run_id": "r", "key": "private"})
        ticket = self.budget.reserve(provider="first")
        current = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.budget.settle(ticket, "0.01", usage={"prompt_tokens": True, "completion_tokens": 2})
        self.assertEqual(self.path.read_bytes(), current)
        self.assertNotEqual(before, current)

    def test_plain_legacy_reservation_still_has_original_shape(self):
        plain = SharedBudget(self.config, ledger_path=self.path, temp_dir=self.store.home / "budget-temp")
        ticket = plain.reserve(provider="legacy-provider")
        row = json.loads(self.path.read_text())["requests"][ticket]
        self.assertEqual(row, {"status": "reserved", "amount_cny": str(plain.reservation), "provider": "legacy-provider"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
