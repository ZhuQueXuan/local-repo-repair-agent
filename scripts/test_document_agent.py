"""Document permissions, version lineage, quoted evidence and durable handoffs."""
from copy import deepcopy
import os
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
from pathlib import Path
import time
import unittest
from unittest.mock import patch
import uuid
from pydantic import ValidationError

from collaboration import CollaborationCoordinator, RoleContext, role_identity
from collaboration_mock import ScriptedRunner
from collaboration_protocol import RoleIdentity, ProtocolError, canonical_bytes, sha256
from collaboration_store import CollaborationStore, StateConflict
from collaboration_panel import create_panel_run, public_policy, collaboration_view
from document_agent import (ConsultationRecord, DocumentAdvice, DocumentBroker, DocumentSnapshot,
    consultation_context, documentation_paths, freeze_documents, validate_consultation)
from review_tools import ROOT, IntegrityError, ReviewCancelled
from test_collaboration_protocol import StageAFixture
from collaboration_demo import BASE, ISSUE


def advice_for(broker, status="answered", refs=None):
    g = broker.snapshot.grant
    return DocumentAdvice(advice_id="advice-" + g.role_run_id, run_id=g.run_id, role_run_id=g.role_run_id,
        snapshot_id=g.snapshot_id, task_digest=g.task_digest, document_tree_digest=g.document_tree_digest,
        status=status, answer="Scripted interpretation; not independent behavior verification.",
        evidence_ids=tuple(broker.evidence) if refs is None else tuple(refs),
        unknowns=() if status == "answered" else ("Unverified applicability or unavailable documentation.",))


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.f = StageAFixture()
        self.identity = RoleIdentity(run_id="doc-run", role_run_id="doc-role", session_id="doc-session", role="document_reader")
        self.snapshot = freeze_documents(self.f.manifest, self.f.baseline, self.f.issue,
            self.f.root / "document_snapshots/doc-run/doc-role", self.identity, "Which compatibility constraint is documented?")
        self.broker = DocumentBroker(self.snapshot, self.identity)

    def read(self):
        return self.broker.dispatch(self.identity, "read_lines", {"snapshot_id": self.snapshot.grant.snapshot_id,
            "document_id": self.snapshot.grant.documents[0].document_id, "start": 1, "end": 1})

    def record(self):
        return ConsultationRecord(advice_id=self.broker.advice.advice_id, run_id=self.identity.run_id,
            relative_directory="document_snapshots/doc-run/doc-role", grant=self.snapshot.grant,
            advice=self.broker.advice, evidence=tuple(self.broker.evidence.values()))

    def test_scope_is_only_registered_documentation(self):
        self.assertEqual(documentation_paths(self.f.manifest), ["docs/rules.md"])
        result = self.broker.dispatch(self.identity, "list_files", {"snapshot_id": self.snapshot.grant.snapshot_id})
        self.assertEqual([d["path"] for d in result["documents"]], ["docs/rules.md"])
        with self.assertRaises(ProtocolError):
            self.broker.dispatch(self.identity, "read_lines", {"snapshot_id": self.snapshot.grant.snapshot_id, "document_id": self.f.doc().document_id, "start": 1, "end": 1})

    def test_shell_write_network_and_recursive_delegation_unavailable(self):
        with patch("subprocess.Popen", side_effect=AssertionError("never execute")) as launch:
            for tool in ("bash", "write", "exec", "fetch", "delegate", "submit_review", "read_report"):
                with self.subTest(tool=tool), self.assertRaises(ProtocolError):
                    self.broker.dispatch(self.identity, tool, {})
            launch.assert_not_called()

    def test_paths_and_extra_fields_are_not_capabilities(self):
        for path in ("../.env", "C:/secret", "//host/share", "file:stream"):
            with self.subTest(path=path), self.assertRaises((ValidationError, ProtocolError)):
                self.broker.dispatch(self.identity, "read_lines", {"snapshot_id": self.snapshot.grant.snapshot_id, "document_id": path, "start": 1, "end": 1})
        with self.assertRaises(ValidationError):
            self.broker.dispatch(self.identity, "list_files", {"snapshot_id": self.snapshot.grant.snapshot_id, "path": ".env"})

    def test_caller_and_snapshot_are_bound(self):
        other = self.identity.model_copy(update={"run_id": "other-run"})
        with self.assertRaises(ProtocolError):
            self.broker.dispatch(other, "list_files", {"snapshot_id": self.snapshot.grant.snapshot_id})
        with self.assertRaises(ProtocolError):
            self.broker.dispatch(self.identity, "list_files", {"snapshot_id": "old-snapshot"})

    def test_answer_requires_actual_issued_citation(self):
        with self.assertRaisesRegex(ProtocolError, "requires_citation"):
            self.broker.dispatch(self.identity, "submit_consultation", {"advice": advice_for(self.broker).model_dump(mode="json")})
        with self.assertRaisesRegex(ProtocolError, "unissued"):
            self.broker.dispatch(self.identity, "submit_consultation", {"advice": advice_for(self.broker, refs=("invented-id",)).model_dump(mode="json")})
        result = self.read()
        self.broker.dispatch(self.identity, "submit_consultation", {"advice": advice_for(self.broker).model_dump(mode="json")})
        context = consultation_context(self.record(), self.f.root)
        self.assertEqual(context["excerpts"][0]["excerpt"], result["content"])
        self.assertIn("model claim", context["scope"])

    def test_old_advice_cannot_be_applied_to_new_document_version(self):
        self.read()
        old = advice_for(self.broker)
        path = self.f.baseline / "docs/rules.md"
        path.write_bytes(b"New behavior constraint.\n")
        manifest = deepcopy(self.f.manifest)
        manifest["source_files_sha256"]["docs/rules.md"] = sha256(path.read_bytes())
        new = freeze_documents(manifest, self.f.baseline, self.f.issue, self.f.root / "new-documents", self.identity, self.snapshot.grant.question)
        broker = DocumentBroker(new, self.identity)
        with self.assertRaisesRegex(ProtocolError, "version_mismatch"):
            broker.dispatch(self.identity, "submit_consultation", {"advice": old.model_dump(mode="json")})

    def test_later_work_and_source_changes_do_not_pollute_snapshot(self):
        before = self.read()["content"]
        (self.f.baseline / "docs/rules.md").write_text("later original change")
        (self.f.work / "docs/rules.md").write_text("later checkout change")
        self.assertEqual(self.read()["content"], before)

    def test_frozen_object_tamper_blocks_further_tools(self):
        doc = self.snapshot.grant.documents[0]
        (self.snapshot.directory / "objects" / doc.document_id).write_text("tampered")
        with self.assertRaises(IntegrityError):
            self.read()
        self.assertTrue(self.broker.blocked)

    def test_forged_citation_span_is_rejected_on_reload(self):
        self.read()
        self.broker.dispatch(self.identity, "submit_consultation", {"advice": advice_for(self.broker).model_dump(mode="json")})
        record = self.record()
        forged = record.model_copy(update={"evidence": (record.evidence[0].model_copy(update={"span_sha256": "0"*64}),)})
        with self.assertRaisesRegex(ProtocolError, "span_mismatch"):
            validate_consultation(forged, self.f.root)

    def test_not_found_and_conflicts_cannot_claim_unsupported_success(self):
        self.broker.dispatch(self.identity, "submit_consultation", {"advice": advice_for(self.broker, "information_not_found").model_dump(mode="json")})
        self.assertFalse(consultation_context(self.record(), self.f.root)["excerpts"])
        broker = DocumentBroker(self.snapshot, self.identity)
        with self.assertRaisesRegex(ProtocolError, "two_citations"):
            broker.dispatch(self.identity, "submit_consultation", {"advice": advice_for(broker, "conflicting_information").model_dump(mode="json")})

    def test_cancel_before_and_after_read_prevents_submission(self):
        broker = DocumentBroker(self.snapshot, self.identity, cancelled=lambda: True)
        with self.assertRaises(ReviewCancelled):
            broker.dispatch(self.identity, "list_files", {"snapshot_id": self.snapshot.grant.snapshot_id})
        with patch.object(DocumentSnapshot, "verify", return_value=None):
            values = iter((False, True))
            broker = DocumentBroker(self.snapshot, self.identity, cancelled=lambda: next(values))
            with self.assertRaises(ReviewCancelled):
                broker.dispatch(self.identity, "list_files", {"snapshot_id": self.snapshot.grant.snapshot_id})

    def test_submission_closes_tools_and_cannot_overwrite_advice(self):
        self.read()
        self.broker.dispatch(self.identity, "submit_consultation", {"advice": advice_for(self.broker).model_dump(mode="json")})
        with self.assertRaisesRegex(ProtocolError, "already_submitted"):
            self.read()

    def test_manifest_hash_changes_and_private_paths_are_rejected(self):
        (self.f.baseline / "docs/rules.md").write_text("different bytes")
        with self.assertRaisesRegex(IntegrityError, "baseline_mismatch"):
            freeze_documents(self.f.manifest, self.f.baseline, self.f.issue, self.f.root / "bad-baseline", self.identity, "Question")
        manifest = deepcopy(self.f.manifest)
        manifest["source_files_sha256"]["docs/.env-secret.md"] = "0"*64
        with self.assertRaisesRegex(ProtocolError, "private_manifest"):
            documentation_paths(manifest)

    def test_source_hardlink_cannot_be_frozen_as_document(self):
        source = self.f.root / "hardlinked-source/docs"
        source.mkdir(parents=True)
        os.link(self.f.baseline / "docs/rules.md", source / "rules.md")
        with self.assertRaisesRegex(IntegrityError, "hardlink"):
            freeze_documents(self.f.manifest, source.parent, self.f.issue, self.f.root / "linked-docs", self.identity, "Question")

    def test_large_document_handoff_is_rejected_before_submission(self):
        raw = ("A"*3000 + "\nB" + "b"*2999 + "\n").encode()
        (self.f.baseline / "docs/rules.md").write_bytes(raw)
        manifest = deepcopy(self.f.manifest)
        manifest["source_files_sha256"]["docs/rules.md"] = sha256(raw)
        snapshot = freeze_documents(manifest, self.f.baseline, self.f.issue, self.f.root / "large-docs", self.identity, "Question")
        broker = DocumentBroker(snapshot, self.identity)
        doc = snapshot.grant.documents[0]
        for n in (1,2):
            broker.dispatch(self.identity, "read_lines", {"snapshot_id": snapshot.grant.snapshot_id, "document_id": doc.document_id, "start": n, "end": n})
        with self.assertRaisesRegex(ProtocolError, "handoff_too_large"):
            broker.dispatch(self.identity, "submit_consultation", {"advice": advice_for(broker).model_dump(mode="json")})
        self.assertIsNone(broker.advice)

    def test_utf8_crlf_quote_and_search_are_byte_exact(self):
        raw = "兼容性必须保留。\r\n边界信息未知。\r\n".encode()
        (self.f.baseline / "docs/rules.md").write_bytes(raw)
        manifest = deepcopy(self.f.manifest)
        manifest["source_files_sha256"]["docs/rules.md"] = sha256(raw)
        snapshot = freeze_documents(manifest, self.f.baseline, self.f.issue, self.f.root / "unicode-docs", self.identity, "Question")
        broker = DocumentBroker(snapshot, self.identity)
        hits = broker.dispatch(self.identity, "search_text", {"snapshot_id": snapshot.grant.snapshot_id, "query": "边界"})["hits"]
        self.assertEqual(hits[0]["content"].encode(), "边界信息未知。\r\n".encode())
        self.assertEqual(hits[0]["evidence"]["span_sha256"], sha256(hits[0]["content"].encode()))

    def test_policy_rejects_empty_questions_and_incompatible_modes(self):
        for question in (None, "", " ", "x"*1001):
            with self.subTest(question=str(question)[:10]), self.assertRaises(ValueError):
                public_policy(documents=True, question=question)
        with self.assertRaises(ValueError):
            public_policy(self_review=True, documents=True, question="Question")


class DocumentStoreTests(unittest.TestCase):
    def test_failed_optional_consultation_continues_but_never_counts_as_success(self):
        from collaboration_live import LiveRunner
        store, parent, _ = self.make_parent()
        live = LiveRunner.__new__(LiveRunner)
        live.store, live.run_id, live.output = store, parent["id"], store.home
        live.parent_deadline = time.monotonic() + 120
        fixture = StageAFixture()
        delegate = ScriptedRunner(store)
        class Runner:
            document_unavailable = live.document_unavailable
            def __call__(self, context):
                if context.role["phase"] != "consulting":
                    self_context = live.document_context()
                    assert self_context["advice"] is None and not self_context["evidence"]
                    return delegate(context)
                live.document_snapshot = freeze_documents(fixture.manifest, fixture.baseline, fixture.issue,
                    store.home / "failed-documents", context.identity, "What is documented?")
                raise ProtocolError("document_repeated_evidence_limit")
        result = CollaborationCoordinator(store, Runner()).run(parent["id"])
        self.assertEqual(result["status"], "finished", result)
        self.assertEqual(result["collaboration_result"], "degraded")
        self.assertEqual(store.roles(parent["id"])[0]["status"], "failed")
        self.assertFalse(store.records(parent["id"], "consultation"))
        self.assertEqual(result["review_verdict"], "approve")

    def test_document_fallback_never_accepts_pending_request_or_undeclared_policy(self):
        for pending in (False, True):
            store, parent, _ = self.make_parent()
            store.claim_parent(parent["id"])
            role = store.claim_role(parent["id"], "consulting", sha256(b"input"), 0)
            revision = store.state(parent["id"])["collaboration_state"]["revision"]
            if pending:
                store.prepare_request(role["id"], "pending", sha256(b"pending"))
            else:
                with store.transaction() as db:
                    value = store.get(parent["id"])["collaboration_policy"]
                    value.pop("document_failure_policy")
                    store._parent(db, parent["id"], collaboration_policy=value)
            with self.assertRaises((StateConflict, ProtocolError)):
                store.continue_without_document(role["id"], "document_repeated_format_errors", revision)
            self.assertEqual(store.state(parent["id"])["collaboration_state"]["phase"], "consulting")

    def make_parent(self):
        store = CollaborationStore(ROOT / "temp/document_agent_tests" / uuid.uuid4().hex)
        manifest = {"source_prefix": "pkg/", "source_files_sha256": {"pkg/main.py": sha256(BASE)}}
        parent = store.create_collaboration("synthetic-arithmetic", "adaptive_history", "docs", public_policy(documents=True, question="What is documented?"),
            input_manifest=manifest, input_issue=ISSUE)
        return store, parent, manifest

    def test_serial_consultation_handoff_and_old_four_stage_flow_coexist(self):
        store, parent, manifest = self.make_parent()
        delegate = ScriptedRunner(store, accounting=True)
        def runner(context):
            if context.role["phase"] != "consulting":
                return delegate(context)
            source = store.home / "source"
            source.mkdir()
            snapshot = freeze_documents(manifest, source, ISSUE, store.home / ("document_snapshots/" + parent["id"] + "/" + context.role["id"]), context.identity, "What is documented?")
            broker = DocumentBroker(snapshot, context.identity)
            advice = advice_for(broker, "information_not_found")
            broker.dispatch(context.identity, "submit_consultation", {"advice": advice.model_dump(mode="json")})
            record = ConsultationRecord(advice_id=advice.advice_id, run_id=parent["id"], relative_directory=snapshot.directory.relative_to(store.home).as_posix(),
                grant=snapshot.grant, advice=advice, evidence=())
            store.register_record(context.role["id"], "consultation", record)
            return {"advice_id": advice.advice_id}
        result = CollaborationCoordinator(store, runner).run(parent["id"])
        self.assertEqual(result["status"], "finished")
        self.assertEqual([r["status"] for r in store.roles(parent["id"])], ["succeeded"]*5)
        self.assertEqual(len(store.records(parent["id"], "consultation")), 1)
        self.assertEqual(collaboration_view(store, parent["id"])["consultations"][0]["advice"]["status"], "information_not_found")
        roles = store.roles(parent["id"])
        self.assertNotIn(roles[0]["session_id"], [r["session_id"] for r in roles[1:]])

    def test_missing_consultation_stops_before_repair(self):
        store, parent, _ = self.make_parent()
        result = CollaborationCoordinator(store, lambda context: {"advice_id": "fabricated"}).run(parent["id"])
        self.assertEqual(result["status"], "failed")
        self.assertTrue(all(r.get("started") is None for r in store.roles(parent["id"])[1:]))

    def test_cancel_and_restart_never_start_repair_or_reissue_consultation(self):
        store, parent, _ = self.make_parent()
        store.claim_parent(parent["id"])
        store.claim_role(parent["id"], "consulting", sha256(b"input"), 0)
        restored = CollaborationStore(store.home)
        restored.reconcile_collaborations()
        self.assertEqual(restored.get(parent["id"])["status"], "interrupted")
        with patch("litellm.completion", side_effect=AssertionError("no resume")) as api:
            CollaborationCoordinator(restored, lambda context: self.fail("no role should start")).run(parent["id"])
            api.assert_not_called()

    def test_public_request_only_accepts_question_in_document_mode(self):
        store, _, _ = self.make_parent()
        with self.assertRaises(ValueError):
            create_panel_run(store, {"collaboration_mode": "single", "document_question": "ignored?"})
        with self.assertRaises(ValueError):
            create_panel_run(store, {"task": "split-after-boundary", "profile": "adaptive_history", "mode": "live", "request_key": "bad", "collaboration_mode": "document_review", "policy_version": 1, "document_question": "x", "document_paths": [".env"]})


if __name__ == "__main__":
    unittest.main()
