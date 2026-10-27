"""Real files, links, snapshots and capability rejection, without model calls."""
import os
from pathlib import Path
import stat
import subprocess
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from collaboration_protocol import (ProgramReport, ProtocolError, RoleIdentity, canonical_bytes, sha256)
from review_tools import IntegrityError, ReviewCancelled, ReviewEvidenceBroker, seal_candidate
from test_collaboration_protocol import StageAFixture


class ReviewToolsTests(unittest.TestCase):
    def setUp(self):
        self.f = StageAFixture()

    def call(self, tool, args):
        return self.f.broker.dispatch(self.f.reviewer, tool, args)

    def read_args(self, **changes):
        return {"snapshot_id": self.f.snapshot.candidate.snapshot_id, "document_id": self.f.doc().document_id, "start": 1, "end": 2} | changes

    def test_read_returns_exact_bytes_lines_and_hashes(self):
        result = self.call("read_lines", self.read_args())
        self.assertEqual(result["content"].encode(), self.f.v1)
        self.assertEqual(result["evidence"]["span_sha256"], sha256(self.f.v1))
        self.assertEqual(result["evidence"]["start_line"], 1)
        self.assertFalse(result["truncated"])

    def test_all_six_tools_operate_and_do_not_change_snapshots_or_work(self):
        before = {p: sha256(p.read_bytes()) for base in [self.f.snapshot.directory, self.f.work] for p in base.rglob("*") if p.is_file()}
        files = self.call("list_files", {"snapshot_id": self.f.snapshot.candidate.snapshot_id})
        self.assertTrue(files["documents"])
        self.assertTrue(self.call("search_text", {"snapshot_id": self.f.snapshot.candidate.snapshot_id, "query": "sum"})["hits"])
        self.assertIn("@@", self.call("read_diff", {"candidate_id": self.f.snapshot.candidate.candidate_id})["content"])
        self.assertEqual(self.call("read_report", {"report_id": self.f.report.report_id})["report"]["origin"], "program_test")
        result = self.call("submit_review", {"report": self.f.review().model_dump(mode="json")})
        self.assertEqual(result["defect_truth"], "not_assessed")
        self.assertEqual(result["repair_result"], "not_assessed")
        self.assertEqual(before, {p: sha256(p.read_bytes()) for base in [self.f.snapshot.directory, self.f.work] for p in base.rglob("*") if p.is_file()})

    def test_execution_and_write_tools_are_rejected_before_any_spawn(self):
        with patch("subprocess.Popen", side_effect=AssertionError("Must never spawn")) as spawn:
            for name in ["bash", "powershell", "write", "exec", "import_module", "delete", "__getattribute__"]:
                with self.subTest(name=name), self.assertRaisesRegex(ProtocolError, "tool_not_permitted"):
                    self.call(name, {"command": "echo forbidden"})
            spawn.assert_not_called()
        self.assertEqual(self.f.broker.events[-1]["kind"], "tool_rejected")

    def test_extra_fields_and_nested_actions_do_not_bypass_dispatch(self):
        for extra in [{"path": "../secret"}, {"command": "echo forbidden"}, {"actions": [{"name": "bash"}]}, {"start": "1"}, {"start": True}, {"schema_version": 2}]:
            with self.subTest(extra=extra), self.assertRaises((ValidationError, ProtocolError)):
                self.call("read_lines", self.read_args(**extra))

    def test_absolute_paths_traversal_unc_and_ads_are_not_document_ids(self):
        for value in ["../.env", "C:/Users/secret", "C:\\Users\\secret", "//server/share", "file:stream", "/etc/passwd"]:
            with self.subTest(value=value), self.assertRaises((ValidationError, ProtocolError)):
                self.call("read_lines", self.read_args(document_id=value))
        for value in ["../", "C:/", "//server", "x:stream", "x\\y"]:
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                self.call("list_files", {"snapshot_id": self.f.snapshot.candidate.snapshot_id, "prefix": value})

    def test_caller_and_snapshot_versions_are_bound(self):
        other = RoleIdentity(run_id="another-run", role_run_id="reviewer-1", session_id="review-session-1", role="reviewer")
        with self.assertRaisesRegex(ProtocolError, "caller_identity_mismatch"):
            self.f.broker.dispatch(other, "read_lines", self.read_args())
        with self.assertRaisesRegex(ProtocolError, "snapshot_identity_mismatch"):
            self.call("read_lines", self.read_args(snapshot_id="another-snapshot"))
        with self.assertRaisesRegex(ProtocolError, "candidate_identity_mismatch"):
            self.call("read_diff", {"candidate_id": "another-candidate"})

    def test_same_document_id_in_another_run_does_not_grant_access(self):
        other = StageAFixture(run_id="run-b")
        self.assertEqual(other.doc().document_id, self.f.doc().document_id)
        with self.assertRaises(ProtocolError):
            self.f.broker.dispatch(other.reviewer, "read_lines", self.read_args())

    def test_handoff_limits_docs_reports_and_capabilities(self):
        handoff = self.f.make_handoff(read_document_ids=(self.f.doc().document_id,), report_ids=())
        broker = ReviewEvidenceBroker(self.f.snapshot, self.f.reviewer, handoff, reports=(self.f.report,), capabilities=("read_lines", "read_report"))
        with self.assertRaises(ProtocolError):
            broker.dispatch(self.f.reviewer, "read_diff", {"candidate_id": self.f.snapshot.candidate.candidate_id})
        with self.assertRaises(ProtocolError):
            broker.dispatch(self.f.reviewer, "read_lines", self.read_args(document_id=self.f.doc("baseline").document_id))
        result = broker.dispatch(self.f.reviewer, "read_report", {"report_id": self.f.report.report_id})
        self.assertEqual(result, {"status": "not_available", "evidence": None})
        with self.assertRaises(ProtocolError):
            ReviewEvidenceBroker(self.f.snapshot, self.f.reviewer, handoff, capabilities=("bash",))

    def test_v1_snapshot_remains_v1_after_real_worktree_changes(self):
        self.f.write(self.f.v2)
        newer = self.f.seal("v2")
        self.assertNotEqual(newer.candidate.source_tree_digest, self.f.snapshot.candidate.source_tree_digest)
        self.assertEqual(self.call("read_lines", self.read_args())["content"].encode(), self.f.v1)
        self.assertEqual(self.f.snapshot.read(self.f.doc("baseline").document_id), self.f.original)

    def test_changed_snapshot_permanently_blocks_review(self):
        path = self.f.snapshot.directory / "objects" / self.f.doc().document_id
        path.write_bytes(b"tampered snapshot\n")
        with self.assertRaises(IntegrityError):
            self.call("read_lines", self.read_args())
        path.write_bytes(self.f.v1)
        with self.assertRaisesRegex(IntegrityError, "review_integrity_blocked"):
            self.call("read_lines", self.read_args())
        self.assertFalse(self.f.broker.reviews)

    def test_changed_snapshot_manifest_is_not_rehashed_as_valid(self):
        (self.f.snapshot.directory / "candidate.json").write_bytes(b"{}")
        with self.assertRaises(IntegrityError):
            self.call("read_lines", self.read_args())

    def test_missing_snapshot_object_stops_with_no_review(self):
        # Rename one exact fixture file; no deletion or recursive cleanup.
        path = self.f.snapshot.directory / "objects" / self.f.doc().document_id
        path.rename(path.with_name(path.name + ".missing"))
        with self.assertRaises((FileNotFoundError, IntegrityError)):
            self.call("read_lines", self.read_args())
        self.assertFalse(self.f.broker.reviews)

    def test_search_is_literal_not_regex_or_executable(self):
        result = self.call("search_text", {"snapshot_id": self.f.snapshot.candidate.snapshot_id, "query": ".*"})
        self.assertEqual(result["hits"], [])
        result = self.call("search_text", {"snapshot_id": self.f.snapshot.candidate.snapshot_id, "query": "SUM", "case_sensitive": True})
        self.assertFalse(result["hits"])
        result = self.call("search_text", {"snapshot_id": self.f.snapshot.candidate.snapshot_id, "query": "SUM"})
        self.assertTrue(result["hits"])

    def test_read_limits_and_long_line_do_not_create_fake_complete_evidence(self):
        for changes in [{"start": 0}, {"end": 1, "start": 2}, {"end": 81}, {"end": 100}]:
            with self.subTest(changes=changes), self.assertRaises((ProtocolError, ValidationError)):
                self.call("read_lines", self.read_args(**changes))
        self.f.write(b"x" * 5000 + b"\n")
        newer = self.f.seal("longline")
        self.f.snapshot = newer
        self.f.report = ProgramReport.parse(self.f.report.model_dump(mode="json") | {"candidate_id": newer.candidate.candidate_id, "source_tree_digest": newer.candidate.source_tree_digest})
        broker = ReviewEvidenceBroker(newer, self.f.reviewer, self.f.make_handoff(), reports=(self.f.report,))
        doc = next(d for d in newer.candidate.documents if d.side == "candidate" and d.path == "pkg/main.py")
        result = broker.dispatch(self.f.reviewer, "read_lines", {"snapshot_id": newer.candidate.snapshot_id, "document_id": doc.document_id, "start": 1, "end": 1})
        self.assertEqual(result["status"], "line_too_large")
        self.assertIsNone(result["evidence"])

    def test_unknown_reports_stay_unavailable(self):
        result = self.call("read_report", {"report_id": "missing-report"})
        self.assertEqual(result["status"], "not_available")
        self.assertIsNone(result["evidence"])

    def test_invalid_finding_does_not_register_a_review(self):
        data = self.f.review().model_dump(mode="json")
        data["findings"][0]["evidence_ids"] = ["missing-evidence"]
        with self.assertRaises(ProtocolError):
            self.call("submit_review", {"report": data})
        self.assertFalse(self.f.broker.reviews)

    def test_submission_is_idempotent_and_conflicting_content_is_rejected(self):
        data = self.f.review().model_dump(mode="json")
        one = self.call("submit_review", {"report": data})
        self.assertEqual(one, self.call("submit_review", {"report": data}))
        self.assertEqual(len(self.f.broker.reviews), 1)
        with self.assertRaises(ProtocolError):
            self.call("submit_review", {"report": data | {"unknowns": ["Changed content"]}})
        with self.assertRaises(ProtocolError):
            self.call("submit_review", {"report": data | {"review_id": "another-review"}})

    def test_cancellation_prevents_tool_access_and_review_submission(self):
        broker = ReviewEvidenceBroker(self.f.snapshot, self.f.reviewer, self.f.handoff, reports=(self.f.report,), cancelled=lambda: True)
        with self.assertRaises(ReviewCancelled):
            broker.dispatch(self.f.reviewer, "read_lines", self.read_args())
        self.assertFalse(broker.evidence)
        self.assertFalse(broker.reviews)

    def test_cancel_during_submission_does_not_commit_review(self):
        checks = iter([False, True])
        self.f.broker.cancelled = lambda: next(checks)
        # Build the review before injecting the two cancellation checkpoints.
        self.f.broker.cancelled = lambda: False
        review = self.f.review()
        self.f.broker.cancelled = lambda: next(checks)
        with self.assertRaises(ReviewCancelled):
            self.call("submit_review", {"report": review.model_dump(mode="json")})
        self.assertFalse(self.f.broker.reviews)

    def test_multi_page_search_and_file_list_are_explicitly_bounded(self):
        self.f.write(b"value = 1\n" * 30)
        newer = self.f.seal("many-lines")
        self.f.snapshot = newer
        self.f.report = ProgramReport.parse(self.f.report.model_dump(mode="json") | {"candidate_id": newer.candidate.candidate_id, "source_tree_digest": newer.candidate.source_tree_digest})
        broker = ReviewEvidenceBroker(newer, self.f.reviewer, self.f.make_handoff(), reports=(self.f.report,))
        docs = [self.f.doc().document_id]
        result = broker.dispatch(self.f.reviewer, "search_text", {"snapshot_id": newer.candidate.snapshot_id, "query": "value", "document_ids": docs})
        self.assertLessEqual(len(canonical_bytes(result)), 4096)
        self.assertGreater(len(result["hits"]), 0)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["search_exhausted"])
        lines = [h["evidence"]["start_line"] for h in result["hits"]]
        while result["next_cursor"] is not None:
            result = broker.dispatch(self.f.reviewer, "search_text", {"snapshot_id": newer.candidate.snapshot_id,
                "query": "value", "cursor": result["next_cursor"], "document_ids": docs})
            self.assertLessEqual(len(canonical_bytes(result)), 4096)
            lines.extend(h["evidence"]["start_line"] for h in result["hits"])
        self.assertEqual(lines, list(range(1, 31)))
        self.assertEqual(len(broker.evidence), len(lines))

    def test_unicode_and_escaping_are_bounded_with_exact_issued_spans(self):
        raw = ('value = "中文\\\\\\\""\n' * 40).encode()
        self.f.write(raw)
        self.f.snapshot = self.f.seal("unicode-receipt")
        broker = ReviewEvidenceBroker(self.f.snapshot, self.f.reviewer, self.f.make_handoff(report_ids=()))
        doc = self.f.doc()
        result = broker.dispatch(self.f.reviewer, "read_lines", {"snapshot_id": self.f.snapshot.candidate.snapshot_id,
            "document_id": doc.document_id, "start": 1, "end": 40})
        self.assertLessEqual(len(canonical_bytes(result)), 4096)
        self.assertEqual(result["evidence"]["span_sha256"], sha256(result["content"].encode()))
        self.assertEqual(len(broker.evidence), 1)

    def test_crlf_diff_is_minimal_while_raw_snapshot_keeps_byte_hash(self):
        original = self.f.original.replace(b"\n", b"\r\n")
        (self.f.baseline / "pkg/main.py").write_bytes(original)
        self.f.manifest["source_files_sha256"]["pkg/main.py"] = sha256(original)
        self.f.write(self.f.v1)
        snapshot = self.f.seal("mixed-newlines")
        diff = next(d for d in snapshot.candidate.documents if d.side == "diff")
        patch = snapshot.read(diff.document_id)
        self.assertNotIn(b"-def total", patch)
        self.assertIn(b"-    return sum(values) / len(values)\n", patch)
        baseline = next(d for d in snapshot.candidate.documents if d.side == "baseline" and d.path == "pkg/main.py")
        self.assertEqual(snapshot.read(baseline.document_id), original)
        snapshot.verify()

    def test_no_newline_patch_replays_with_real_git(self):
        self.f.sources["pkg/main.py"] = b"value = 1"
        (self.f.baseline / "pkg/main.py").write_bytes(b"value = 1")
        self.f.manifest["source_files_sha256"]["pkg/main.py"] = sha256(b"value = 1")
        self.f.write(b"value = 2")
        snapshot = self.f.seal("without-newline")
        diff = next(doc for doc in snapshot.candidate.documents if doc.side == "diff")
        patch_path = self.f.root / "no-newline.diff"
        patch_path.write_bytes(snapshot.read(diff.document_id))
        subprocess.run(["git", "init", "--quiet", str(self.f.baseline)], check=True, capture_output=True)
        result = subprocess.run(["git", "-C", str(self.f.baseline), "apply", "--", str(patch_path)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.f.baseline / "pkg/main.py").read_bytes(), b"value = 2")

    def test_existing_real_task_manifest_can_be_sealed_without_model(self):
        from task_workspace import load_task, prepare_task
        source, manifest = load_task("split-after-boundary")
        work, _ = prepare_task("split-after-boundary")
        before = {p: sha256((source / "repo" / p).read_bytes()) for p in manifest["source_files_sha256"]}
        snapshot = seal_candidate(manifest, source / "repo", work, (source / "issue.md").read_bytes(), self.f.root / "real-task", run_id="real-manifest-offline", producer_role_run_id="no-model")
        self.assertTrue(any(doc.path.startswith("docs/") for doc in snapshot.candidate.documents))
        self.assertEqual(before, {p: sha256((source / "repo" / p).read_bytes()) for p in manifest["source_files_sha256"]})
        diff = next(doc for doc in snapshot.candidate.documents if doc.side == "diff")
        self.assertEqual(snapshot.read(diff.document_id), b"")

    def test_case_collisions_and_private_manifest_files_are_rejected(self):
        for relative in [".env", "temp/helper.py", ".git/config", "pkg/MAIN.py"]:
            manifest = dict(self.f.manifest, source_files_sha256=self.f.manifest["source_files_sha256"] | {relative: "0" * 64})
            with self.subTest(relative=relative), self.assertRaises(ProtocolError):
                seal_candidate(manifest, self.f.baseline, self.f.work, self.f.issue, self.f.root / "bad", run_id=self.f.run_id, producer_role_run_id="repairer-1")

    def test_new_files_changed_tests_issue_and_baseline_are_rejected(self):
        (self.f.work / "pkg/new.py").write_bytes(b"new file")
        with self.assertRaisesRegex(IntegrityError, "new_file_outside_manifest"):
            self.f.seal("new-file")
        f = StageAFixture()
        (f.work / "tests/test_main.py").write_bytes(b"changed protected test")
        with self.assertRaisesRegex(IntegrityError, "protected_manifest_file_changed"):
            f.seal("test-change")
        f = StageAFixture()
        (f.work / "TASK.md").write_bytes(b"changed task")
        with self.assertRaisesRegex(IntegrityError, "protected_issue_changed"):
            f.seal("issue-change")
        f = StageAFixture()
        (f.baseline / "pkg/main.py").write_bytes(b"changed baseline")
        with self.assertRaisesRegex(IntegrityError, "baseline_digest_mismatch"):
            f.seal("base-change")

    def test_hardlinks_are_not_exported_as_snapshot_objects(self):
        target = self.f.work / "pkg/main.py"
        alias = self.f.root / "hardlink.py"
        os.link(target, alias)
        with self.assertRaisesRegex(IntegrityError, "hardlink_or_oversized_file"):
            self.f.seal("hardlink")

    def test_real_directory_link_cannot_escape_source_or_output(self):
        outside = self.f.root / "outside"
        outside.mkdir()
        (outside / "secret.py").write_bytes(b"unrelated data")
        link = self.f.root / "linked"
        if os.name == "nt":
            result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
            if result.returncode:
                self.skipTest("Windows junction creation unavailable; reparse check separately covered")
        else:
            link.symlink_to(outside, target_is_directory=True)
        self.assertEqual((outside / "secret.py").read_bytes(), b"unrelated data")
        with self.assertRaisesRegex(IntegrityError, "linked_or_reparse_path"):
            seal_candidate(self.f.manifest, self.f.baseline, link, self.f.issue, self.f.root / "linked-source", run_id=self.f.run_id, producer_role_run_id="repairer-1")
        with self.assertRaisesRegex(IntegrityError, "linked_or_reparse_path"):
            seal_candidate(self.f.manifest, self.f.baseline, self.f.work, self.f.issue, link / "snapshot", run_id=self.f.run_id, producer_role_run_id="repairer-1")
        self.assertFalse((outside / "snapshot").exists())

    def test_reparse_attribute_is_rejected_without_following(self):
        from types import SimpleNamespace
        from review_tools import _unlinked
        with patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=1024)):
            with self.assertRaisesRegex(IntegrityError, "linked_or_reparse_path"):
                _unlinked(self.f.work)

    def test_real_file_symlink_is_not_read(self):
        link = self.f.work / "pkg/link.py"
        try:
            link.symlink_to(self.f.root / "missing-private-file")
        except OSError:
            self.skipTest("File symlink permission unavailable; real directory junction tested separately")
        with self.assertRaises(IntegrityError):
            self.f.seal("symlink")

    def test_snapshot_refuses_reusing_an_existing_output_directory(self):
        with self.assertRaises(FileExistsError):
            self.f.seal("v1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
