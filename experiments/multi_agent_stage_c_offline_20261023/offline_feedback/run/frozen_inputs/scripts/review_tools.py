"""Manifest-bound snapshots and six read-only review operations.

No shell, dynamic imports, executable probes, model SDK or production store.
Trusted callers create snapshots/grants; tool requests can only refer to IDs.
"""
from dataclasses import dataclass
import difflib
import os
from pathlib import Path
import stat
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import Field

from collaboration_protocol import (Candidate, Digest, EvidenceRef, Handoff, Identifier,
    Message, ProgramReport, ProtocolError, READ_TOOLS, ReviewReport, RoleIdentity,
    SourceRef, Text, bind_version, canonical_bytes, issue_requirements, relative_path,
    sha256, tree_digest, validate_evidence, validate_handoff, validate_review)

ROOT = Path(__file__).resolve().parent.parent
MAX_FILE_BYTES = 262144
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024


class IntegrityError(ProtocolError):
    pass


class ReviewCancelled(ProtocolError):
    pass


def _unlinked(path: Path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
        raise IntegrityError("linked_or_reparse_path")
    return info


def _root(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        _unlinked(part)
    if not path.is_relative_to(ROOT) or not path.is_dir():
        raise IntegrityError("root_outside_project")
    return path


def _safe_file(root: Path, relative: str) -> Path:
    relative_path(relative)
    _root(root)
    path = root
    for part in relative.split("/"):
        path /= part
        _unlinked(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise IntegrityError("not_a_regular_file")
    return path


def _read(root: Path, relative: str) -> bytes:
    path = _safe_file(root, relative)
    before = path.stat()
    if before.st_nlink > 1 or before.st_size > MAX_FILE_BYTES:
        raise IntegrityError("hardlink_or_oversized_file")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise IntegrityError("file_changed_during_open")
        raw = stream.read(MAX_FILE_BYTES + 1)
    after = _safe_file(root, relative).stat()
    if len(raw) > MAX_FILE_BYTES or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
        raise IntegrityError("file_changed_during_read")
    raw.decode("utf-8")  # No import, execution, lossy conversion or binary probes.
    return raw


def _output(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if not any(path.is_relative_to(ROOT / name) for name in ("temp", "data")):
        raise IntegrityError("snapshot_output_outside_data_or_temp")
    # Check every existing parent before mkdir, not just the resolved target.
    for part in (*reversed(path.parents), path):
        if part.exists() or part.is_symlink():
            _unlinked(part)
    path.mkdir(parents=True, exist_ok=False)
    _root(path)
    return path


def _private_path(relative):
    parts = relative.casefold().split("/")
    return any(part in {".git", "temp", "__pycache__", "node_modules", ".venv"} or part.startswith(".env") for part in parts)


@dataclass(frozen=True)
class ReviewSnapshot:
    directory: Path
    candidate: Candidate
    manifest_sha256: str
    requirements: tuple

    def verify(self):
        if sha256(_read(self.directory, "candidate.json")) != self.manifest_sha256:
            raise IntegrityError("snapshot_manifest_changed")
        for doc in self.candidate.documents:
            if sha256(_read(self.directory, "objects/" + doc.document_id)) != doc.sha256:
                raise IntegrityError("snapshot_object_changed")

    def read(self, document_id):
        doc = next((doc for doc in self.candidate.documents if doc.document_id == document_id), None)
        if doc is None:
            raise ProtocolError("document_not_available")
        raw = _read(self.directory, "objects/" + doc.document_id)
        if sha256(raw) != doc.sha256:
            raise IntegrityError("snapshot_object_changed")
        return raw


def seal_candidate(manifest: dict, baseline_root: Path, work: Path, issue: bytes, output: Path, *, run_id: str, producer_role_run_id: str) -> ReviewSnapshot:
    """Copy regular manifest files into independent objects; never follow links.

    The manifest and issue bytes are trusted program inputs. This stage does not
    register arbitrary document folders or run the independent test evaluator.
    """
    baseline_root, work = _root(baseline_root), _root(work)
    files = manifest["source_files_sha256"]
    if not files or len(files) > 2000:
        raise ProtocolError("invalid_manifest_size")
    prefix = relative_path(manifest["source_prefix"].rstrip("/")) + "/"
    if len({p.casefold() for p in files}) != len(files):
        raise ProtocolError("case_colliding_manifest_paths")
    baseline, current = {}, {}
    total = len(issue)
    for relative, expected in sorted(files.items()):
        relative_path(relative)
        if _private_path(relative):
            raise ProtocolError("private_manifest_path")
        baseline[relative] = _read(baseline_root, relative)
        current[relative] = _read(work, relative)
        if sha256(baseline[relative]) != expected:
            raise IntegrityError("baseline_digest_mismatch")
        if not relative.startswith(prefix) and baseline[relative] != current[relative]:
            raise IntegrityError("protected_manifest_file_changed")
        total += len(baseline[relative]) + len(current[relative])
    if total > MAX_SNAPSHOT_BYTES or len(issue) > 16384:
        raise ProtocolError("snapshot_too_large")
    # Reject new files outside temp/.git/task_checks; do not traverse reparse dirs.
    for directory, dirs, names in os.walk(work, followlinks=False):
        parent = Path(directory)
        for name in dirs[:]:
            _unlinked(parent / name)
            if parent == work and name in {"temp", ".git", "task_checks"}:
                dirs.remove(name)
        for name in names:
            path = parent / name
            _unlinked(path)
            relative = path.relative_to(work).as_posix()
            if relative not in files and relative != "TASK.md":
                raise IntegrityError("new_file_outside_manifest")
    if (work / "TASK.md").exists() and _read(work, "TASK.md") != issue:
        raise IntegrityError("protected_issue_changed")
    for relative, expected in manifest.get("task_files_sha256", {}).items():
        if relative == "issue.md":
            if sha256(issue) != expected:
                raise IntegrityError("issue_digest_mismatch")
        elif relative == "checks/test_behavior.py":
            if sha256(_read(work, "task_checks/test_behavior.py")) != expected:
                raise IntegrityError("protected_behavior_checks_changed")
        else:
            raise ProtocolError("unsupported_task_protection_entry")
    implementation = {p: sha256(raw) for p, raw in current.items() if p.startswith(prefix)}
    if not implementation:
        raise ProtocolError("empty_implementation_scope")
    patch = []
    for path in sorted(implementation):
        if baseline[path] != current[path]:
            lines = difflib.unified_diff(baseline[path].decode("utf-8").splitlines(keepends=True), current[path].decode("utf-8").splitlines(keepends=True), fromfile="a/" + path, tofile="b/" + path)
            for line in lines:
                patch.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    # Inputs may contain CRLF; patch storage is explicitly LF on Windows too.
    patch_bytes = "".join(patch).replace("\r\n", "\n").encode("utf-8")
    task_digest = sha256(canonical_bytes({"manifest": manifest, "issue_sha256": sha256(issue)}))
    base_digest = tree_digest({p: sha256(raw) for p, raw in baseline.items()})
    source_digest = tree_digest(implementation)
    identity = sha256(canonical_bytes([run_id, task_digest, base_digest, source_digest, sha256(patch_bytes)]))
    candidate_id, snapshot_id = "candidate-" + identity[:32], "snapshot-" + identity[:32]
    objects = []
    for side, paths in (("baseline", baseline), ("candidate", current), ("issue", {"TASK.md": issue}), ("diff", {"repair.diff": patch_bytes})):
        for path, raw in sorted(paths.items()):
            doc_id = "doc-" + sha256(canonical_bytes([side, path]))[:32]
            if len(raw) > MAX_FILE_BYTES:
                raise ProtocolError("document_too_large")
            objects.append((SourceRef(document_id=doc_id, path=path, side=side, sha256=sha256(raw), byte_count=len(raw), line_count=len(raw.decode("utf-8").splitlines())), raw))
    candidate = Candidate(candidate_id=candidate_id, run_id=run_id, producer_role_run_id=producer_role_run_id,
                          task_digest=task_digest, baseline_digest=base_digest, source_tree_digest=source_digest,
                          patch_sha256=sha256(patch_bytes), snapshot_id=snapshot_id, documents=tuple(doc for doc, raw in objects))
    requirements = issue_requirements(issue.decode("utf-8"))
    destination = _output(output)
    (destination / "objects").mkdir()
    for doc, raw in objects:
        with (destination / "objects" / doc.document_id).open("xb") as stream:
            stream.write(raw)
    manifest_bytes = canonical_bytes(candidate)
    with (destination / "candidate.json").open("xb") as stream:
        stream.write(manifest_bytes)
    snapshot = ReviewSnapshot(destination, candidate, sha256(manifest_bytes), requirements)
    snapshot.verify()
    return snapshot


class ListFiles(Message):
    snapshot_id: Identifier
    prefix: Annotated[str, Field(max_length=240)] = ""
    cursor: Annotated[int, Field(ge=0)] = 0


class ReadLines(Message):
    snapshot_id: Identifier
    document_id: Identifier
    start: Annotated[int, Field(ge=1)]
    end: Annotated[int, Field(ge=1)]


class SearchText(Message):
    snapshot_id: Identifier
    query: Annotated[str, Field(min_length=1, max_length=200)]
    document_ids: Annotated[tuple[Identifier, ...], Field(max_length=200)] = ()
    case_sensitive: bool = False


class ReadDiff(Message):
    candidate_id: Identifier
    cursor: Annotated[int, Field(ge=0)] = 0


class ReadReport(Message):
    report_id: Identifier
    section: Literal["all", "status", "scope", "summary"] = "all"


class SubmitReview(Message):
    report: ReviewReport


SCHEMAS = {"list_files": ListFiles, "read_lines": ReadLines, "search_text": SearchText,
           "read_diff": ReadDiff, "read_report": ReadReport, "submit_review": SubmitReview}


class ReviewEvidenceBroker:
    def __init__(self, snapshot: ReviewSnapshot, reviewer: RoleIdentity, handoff: Handoff, *, reports=(), capabilities=READ_TOOLS, cancelled=lambda: False):
        self.snapshot, self.reviewer = snapshot, reviewer
        self.candidate = snapshot.candidate
        self._documents = MappingProxyType({doc.document_id: doc for doc in self.candidate.documents})
        self._reports = {}
        for report in reports:
            report = ProgramReport.parse(report)
            bind_version(report, self.candidate)
            if report.report_id in self._reports:
                raise ProtocolError("duplicate_report")
            self._reports[report.report_id] = report
        self._reports = MappingProxyType(self._reports)
        self._evidence, self._reviews = {}, {}
        self._events, self._blocked = [], False
        self.cancelled = cancelled
        self.capabilities = frozenset(capabilities)
        if not self.capabilities <= SCHEMAS.keys():
            raise ProtocolError("unknown_capability")
        # Stage A starts without cross-session evidence; B will provide a registry.
        validate_handoff(handoff, self.candidate, reviewer, self._reports, self._evidence)
        self.allowed_documents = frozenset(handoff.read_document_ids)
        self.allowed_reports = frozenset(handoff.report_ids)
        self.snapshot.verify()

    @property
    def documents(self):
        return self._documents

    @property
    def reports(self):
        return self._reports

    @property
    def evidence(self):
        return MappingProxyType(self._evidence)

    @property
    def reviews(self):
        return MappingProxyType(self._reviews)

    @property
    def events(self):
        return tuple(dict(event) for event in self._events)

    def _event(self, kind, **fields):
        self._events.append({"seq": len(self._events) + 1, "kind": kind, "actor": self.reviewer.role,
                             "role_run_id": self.reviewer.role_run_id, "candidate_id": self.candidate.candidate_id, **fields})

    def _doc(self, document_id):
        if document_id not in self.allowed_documents:
            raise ProtocolError("document_outside_handoff_scope")
        return self._documents[document_id]

    def _source_evidence(self, doc, start, end, raw):
        fields = {"run_id": self.candidate.run_id, "candidate_id": self.candidate.candidate_id,
                  "source_tree_digest": self.candidate.source_tree_digest, "producer_role_run_id": self.reviewer.role_run_id,
                  "producer_event_seq": len(self._events), "kind": "diff" if doc.side == "diff" else "source",
                  "document_id": doc.document_id, "path": doc.path, "side": doc.side, "start_line": start,
                  "end_line": end, "file_sha256": doc.sha256, "span_sha256": sha256(raw)}
        ref = EvidenceRef(evidence_id="ev-" + sha256(canonical_bytes(fields))[:32], **fields)
        self._evidence[ref.evidence_id] = ref
        return ref

    def _lines(self, doc, start, end):
        if end < start or end - start + 1 > 80 or end > doc.line_count:
            raise ProtocolError("invalid_line_range")
        lines = self.snapshot.read(doc.document_id).decode("utf-8").splitlines(keepends=True)
        selected = []
        size = 0
        for line in lines[start - 1:end]:
            encoded = line.encode("utf-8")
            if size + len(encoded) > 4096:
                break
            selected.append(line)
            size += len(encoded)
        if not selected:
            return {"status": "line_too_large", "document_id": doc.document_id, "truncated": True, "evidence": None}
        raw = "".join(selected).encode("utf-8")
        actual_end = start + len(selected) - 1
        ref = self._source_evidence(doc, start, actual_end, raw)
        return {"status": "ok", "content": raw.decode("utf-8"), "evidence": ref.model_dump(mode="json"),
                "truncated": actual_end < end, "next_line": actual_end + 1 if actual_end < end else None}

    def validate_issued_evidence(self, evidence):
        """Trusted registry rejects forged spans even if their shape is valid."""
        ref = EvidenceRef.parse(evidence)
        issued = self._evidence.get(ref.evidence_id)
        if issued is None or issued != ref:
            raise ProtocolError("evidence_not_issued_or_altered")
        validate_evidence(ref, self.candidate, self.documents, self.reports)
        return ref

    def dispatch(self, identity: RoleIdentity, tool_name: str, arguments: dict):
        try:
            if identity != self.reviewer:
                raise ProtocolError("caller_identity_mismatch")
            if self.cancelled():
                raise ReviewCancelled("review_cancelled")
            if self._blocked:
                raise IntegrityError("review_integrity_blocked")
            if not isinstance(tool_name, str) or tool_name not in self.capabilities or tool_name not in SCHEMAS:
                raise ProtocolError("tool_not_permitted")
            if len(canonical_bytes(arguments)) > 32768:
                raise ProtocolError("tool_request_too_large")
            request = SCHEMAS[tool_name].parse(arguments)
            if hasattr(request, "snapshot_id") and request.snapshot_id != self.candidate.snapshot_id:
                raise ProtocolError("snapshot_identity_mismatch")
            if hasattr(request, "candidate_id") and request.candidate_id != self.candidate.candidate_id:
                raise ProtocolError("candidate_identity_mismatch")
            self.snapshot.verify()
            self._event("tool_started", tool=tool_name)
            result = self._execute(tool_name, request)
            self.snapshot.verify()
            if self.cancelled():
                raise ReviewCancelled("review_cancelled")
            if tool_name == "submit_review":
                self._reviews[request.report.review_id] = request.report
            self._event("tool_finished", tool=tool_name)
            return result
        except Exception as error:
            if isinstance(error, (IntegrityError, OSError)):
                self._blocked = True
            self._event("tool_rejected", tool=tool_name if isinstance(tool_name, str) and tool_name in SCHEMAS else "unknown", reason=type(error).__name__)
            raise

    def _execute(self, name, request):
        if name == "list_files":
            if request.prefix:
                relative_path(request.prefix.rstrip("/"))
            docs = [d.model_dump(mode="json") for d in self.candidate.documents if d.document_id in self.allowed_documents and d.path.startswith(request.prefix)]
            end = request.cursor + 100
            return {"documents": docs[request.cursor:end], "next_cursor": end if end < len(docs) else None}
        if name == "read_lines":
            return self._lines(self._doc(request.document_id), request.start, request.end)
        if name == "search_text":
            ids = request.document_ids or tuple(sorted(self.allowed_documents))
            query = request.query if request.case_sensitive else request.query.casefold()
            hits, payload_size, truncated = [], 0, False
            for document_id in ids:
                doc = self._doc(document_id)
                for number, line in enumerate(self.snapshot.read(document_id).decode("utf-8").splitlines(keepends=True), 1):
                    if query not in (line if request.case_sensitive else line.casefold()):
                        continue
                    if len(hits) >= 20 or payload_size + len(line.encode("utf-8")) > 4096:
                        truncated = True
                        break
                    ref = self._source_evidence(doc, number, number, line.encode("utf-8"))
                    hits.append({"content": line, "evidence": ref.model_dump(mode="json")})
                    payload_size += len(line.encode("utf-8"))
                if truncated:
                    break
            return {"hits": hits, "truncated": truncated, "search_exhausted": not truncated}
        if name == "read_diff":
            doc = next(doc for doc in self.candidate.documents if doc.side == "diff")
            self._doc(doc.document_id)
            if request.cursor >= doc.line_count:
                return {"status": "empty_or_end", "next_cursor": None, "patch_sha256": self.candidate.patch_sha256}
            result = self._lines(doc, request.cursor + 1, min(doc.line_count, request.cursor + 80))
            end = (result.get("evidence") or {}).get("end_line", request.cursor)
            return result | {"next_cursor": end if end < doc.line_count else None, "patch_sha256": self.candidate.patch_sha256,
                             "baseline_digest": self.candidate.baseline_digest, "source_tree_digest": self.candidate.source_tree_digest}
        if name == "read_report":
            if request.report_id not in self.allowed_reports:
                return {"status": "not_available", "evidence": None}
            report = self.reports[request.report_id]
            fields = {"run_id": report.run_id, "candidate_id": report.candidate_id, "source_tree_digest": report.source_tree_digest,
                      "producer_role_run_id": self.reviewer.role_run_id, "producer_event_seq": len(self._events),
                      "kind": report.origin, "report_id": report.report_id, "report_sha256": sha256(canonical_bytes(report))}
            ref = EvidenceRef(evidence_id="ev-" + sha256(canonical_bytes(fields))[:32], **fields)
            self._evidence[ref.evidence_id] = ref
            content = report.model_dump(mode="json") if request.section == "all" else {request.section: getattr(report, request.section)}
            return {"status": "ok", "report": content, "evidence": ref.model_dump(mode="json")}
        if name == "submit_review":
            review = request.report
            validate_review(review, self.candidate, self.reviewer, self.evidence, self.documents, self.reports, self.snapshot.requirements)
            for finding in review.findings:
                for evidence_id in finding.evidence_ids:
                    self.validate_issued_evidence(self.evidence[evidence_id])
            previous = self._reviews.get(review.review_id)
            if previous is not None and previous != review:
                raise ProtocolError("review_id_content_conflict")
            if self._reviews and previous is None:
                raise ProtocolError("review_already_submitted")
            return {"status": "submitted", "review_id": review.review_id, "verdict": review.verdict,
                    "provenance_validated": True, "defect_truth": "not_assessed", "repair_result": "not_assessed"}
        raise ProtocolError("tool_not_permitted")
