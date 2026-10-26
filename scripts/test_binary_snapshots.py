"""Opaque files stay byte-exact; text tools cannot reinterpret them as evidence."""
import unittest
import shutil
import uuid
from collaboration_protocol import ProtocolError, sha256
from review_tools import ROOT, ReviewEvidenceBroker, IntegrityError, _read_bytes, seal_candidate, is_opaque
from test_collaboration_protocol import StageAFixture


class BinarySnapshotTests(unittest.TestCase):
    def fixture(self, raw=b"\x89PNG\r\n\x1a\n\0binary", path="artwork/logo.png"):
        f = StageAFixture()
        for root in (f.baseline, f.work):
            p = root / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(raw)
        f.manifest["source_files_sha256"][path] = sha256(raw)
        f.snapshot = f.seal("binary")
        f.handoff = f.make_handoff(report_ids=())
        f.broker = ReviewEvidenceBroker(f.snapshot, f.reviewer, f.handoff)
        return f

    def test_opaque_bytes_persist_and_text_search_continues(self):
        f = self.fixture()
        doc = f.doc(path="artwork/logo.png")
        self.assertEqual(doc.line_count, 0)
        self.assertEqual(f.snapshot.read(doc.document_id), b"\x89PNG\r\n\x1a\n\0binary")
        files = f.broker.dispatch(f.reviewer, "list_files", {"snapshot_id": f.snapshot.candidate.snapshot_id})
        self.assertEqual(next(d for d in files["documents"] if d["document_id"] == doc.document_id)["content_kind"], "opaque")
        self.assertTrue(f.broker.dispatch(f.reviewer, "search_text", {"snapshot_id": f.snapshot.candidate.snapshot_id, "query": "sum"})["hits"])
        with self.assertRaisesRegex(ProtocolError, "opaque_document"):
            f.broker.dispatch(f.reviewer, "read_lines", {"snapshot_id": f.snapshot.candidate.snapshot_id, "document_id": doc.document_id, "start": 1, "end": 1})
        self.assertFalse(any(e.document_id == doc.document_id for e in f.broker.evidence.values()))

    def test_opaque_snapshot_survives_later_workspace_changes(self):
        f = self.fixture()
        raw = f.snapshot.read(f.doc(path="artwork/logo.png").document_id)
        (f.work / "artwork/logo.png").write_bytes(b"changed later")
        self.assertEqual(f.snapshot.read(f.doc(path="artwork/logo.png").document_id), raw)
        with self.assertRaisesRegex(IntegrityError, "protected_manifest"):
            f.seal("changed-protected")

    def test_corrupt_opaque_object_blocks_broker(self):
        f = self.fixture()
        (f.snapshot.directory / "objects" / f.doc(path="artwork/logo.png").document_id).write_bytes(b"corrupt")
        with self.assertRaises(IntegrityError):
            f.broker.dispatch(f.reviewer, "list_files", {"snapshot_id": f.snapshot.candidate.snapshot_id})
        self.assertTrue(f.broker._blocked)

    def test_binary_implementation_changes_are_explicitly_unsupported(self):
        f = self.fixture(path="pkg/data.bin")
        (f.work / "pkg/data.bin").write_bytes(b"\0different")
        with self.assertRaisesRegex(ProtocolError, "binary_implementation_changes_unsupported"):
            f.seal("binary-change")

    def test_nul_containing_utf8_is_also_opaque(self):
        f = self.fixture(raw=b"sum\0should not become text")
        result = f.broker.dispatch(f.reviewer, "search_text", {"snapshot_id": f.snapshot.candidate.snapshot_id, "query": "should not"})
        self.assertFalse(result["hits"])

    def test_text_candidate_identity_and_serialization_remain_identical(self):
        f = StageAFixture()
        other = f.seal("text-identical")
        self.assertEqual(f.snapshot.candidate, other.candidate)
        self.assertEqual((f.snapshot.directory / "candidate.json").read_bytes(), (other.directory / "candidate.json").read_bytes())
        self.assertNotIn(b"content_kind", (other.directory / "candidate.json").read_bytes())

    def test_binary_reader_retains_link_and_size_protection(self):
        f = StageAFixture()
        (f.work / "oversize.bin").write_bytes(b"\0" * 262145)
        with self.assertRaises(IntegrityError):
            _read_bytes(f.work, "oversize.bin")
        with self.assertRaises(ProtocolError):
            _read_bytes(f.work, "../baseline/pkg/main.py")

    def test_existing_click_repository_with_real_artwork_can_be_sealed(self):
        from task_workspace import load_task
        source, manifest = load_task("click-custom-command")
        work = ROOT / "temp/binary_click_snapshot" / uuid.uuid4().hex
        work.mkdir(parents=True)
        for relative in manifest["source_files_sha256"]:
            p = work / relative
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / "repo" / relative, p)
        issue = (source / "issue.md").read_bytes()
        (work / "TASK.md").write_bytes(issue)
        (work / "task_checks").mkdir()
        shutil.copyfile(source / "checks/test_behavior.py", work / "task_checks/test_behavior.py")
        snapshot = seal_candidate(manifest, source / "repo", work, issue, work.parent / ("snapshot-" + uuid.uuid4().hex),
            run_id="binary-click-offline", producer_role_run_id="repairer-click")
        opaque = [d for d in snapshot.candidate.documents if is_opaque(d)]
        self.assertTrue(any(d.path.endswith(".png") for d in opaque))
        for doc in opaque:
            self.assertEqual(sha256(snapshot.read(doc.document_id)), manifest["source_files_sha256"][doc.path])
        snapshot.verify()


if __name__ == "__main__":
    unittest.main()
