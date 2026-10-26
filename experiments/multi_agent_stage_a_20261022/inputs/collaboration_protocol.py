"""Versioned, immutable collaboration messages. No execution or model access.

Validation establishes provenance and protocol consistency, never the truth of a
model's reasoning. Registries and capabilities must be supplied by trusted code.
"""
import hashlib
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Text = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=300)]
ToolName = Literal["list_files", "read_lines", "search_text", "read_diff", "read_report", "submit_review"]
READ_TOOLS = ("list_files", "read_lines", "search_text", "read_diff", "read_report", "submit_review")


class ProtocolError(ValueError):
    """A protocol or provenance check failed; no action may be scheduled."""


def canonical_bytes(value) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def tree_digest(files: dict[str, str]) -> str:
    return sha256(canonical_bytes(files))


def relative_path(value: str) -> str:
    """One canonical POSIX spelling, portable to Windows without ADS/aliases."""
    if not isinstance(value, str) or not value or len(value) > 240:
        raise ProtocolError("invalid_relative_path")
    if any(ord(c) < 32 or c in '\\:*?"<>|' for c in value):
        raise ProtocolError("invalid_relative_path")
    parts = value.split("/")
    devices = {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)), *("LPT" + str(i) for i in range(1, 10))}
    if any(p in {"", ".", ".."} or p.endswith((" ", ".")) or p.split(".")[0].upper() in devices for p in parts):
        raise ProtocolError("invalid_relative_path")
    return value


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Annotated[int, Field(strict=True, ge=1, le=1)] = 1

    @model_validator(mode="after")
    def readonly_capability_flags(self):
        # Strict bool fields plus a value check, instead of Literal[False],
        # which would also accept numerically equal integer zero.
        if getattr(self, "write", False) or getattr(self, "execute", False):
            raise ProtocolError("handoff_cannot_grant_write_or_execute")
        return self

    @classmethod
    def parse(cls, payload):
        # JSON arrays become immutable tuples, while strict scalars reject bool
        # as int, stringified integers, unknown fields and unsupported versions.
        return cls.model_validate_json(canonical_bytes(payload))


class SourceRef(Message):
    document_id: Identifier
    path: Text
    side: Literal["baseline", "candidate", "issue", "diff"]
    sha256: Digest
    byte_count: Annotated[int, Field(ge=0, le=262144)]
    line_count: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def path_is_canonical(self):
        relative_path(self.path)
        return self


class Candidate(Message):
    candidate_id: Identifier
    run_id: Identifier
    producer_role_run_id: Identifier
    task_digest: Digest
    baseline_digest: Digest
    source_tree_digest: Digest
    patch_sha256: Digest
    snapshot_id: Identifier
    documents: Annotated[tuple[SourceRef, ...], Field(min_length=1, max_length=4096)]

    @model_validator(mode="after")
    def unique_documents(self):
        ids = [doc.document_id for doc in self.documents]
        paths = [(doc.side, doc.path.casefold()) for doc in self.documents]
        if len(set(ids)) != len(ids) or len(set(paths)) != len(paths):
            raise ProtocolError("duplicate_document")
        return self


class RoleIdentity(Message):
    run_id: Identifier
    role_run_id: Identifier
    session_id: Identifier
    role: Identifier  # Not a two-role enum: future read-only specialists reuse it.


class Requirement(Message):
    requirement_id: Identifier
    text: Text


def issue_requirements(issue: str) -> tuple[Requirement, ...]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", issue.strip()) if part.strip()]
    if not paragraphs or len(paragraphs) > 100:
        raise ProtocolError("invalid_issue_paragraphs")
    return tuple(Requirement(requirement_id=f"issue:paragraph-{i}", text=part) for i, part in enumerate(paragraphs, 1))


class ProgramReport(Message):
    report_id: Identifier
    run_id: Identifier
    candidate_id: Identifier
    source_tree_digest: Digest
    producer_role_run_id: Identifier
    origin: Literal["program_test", "model_claim"]
    status: Literal["passed", "failed", "unknown", "not_run"]
    scope: Text
    summary: Text


class EvidenceRef(Message):
    evidence_id: Identifier
    run_id: Identifier
    candidate_id: Identifier
    source_tree_digest: Digest
    producer_role_run_id: Identifier
    producer_event_seq: Annotated[int, Field(ge=1)]
    kind: Literal["source", "diff", "program_test", "model_claim"]
    document_id: Identifier | None = None
    path: Text | None = None
    side: Literal["baseline", "candidate", "issue", "diff"] | None = None
    start_line: Annotated[int, Field(ge=1)] | None = None
    end_line: Annotated[int, Field(ge=1)] | None = None
    file_sha256: Digest | None = None
    span_sha256: Digest | None = None
    report_id: Identifier | None = None
    report_sha256: Digest | None = None

    @model_validator(mode="after")
    def shape(self):
        source_fields = (self.document_id, self.path, self.side, self.start_line, self.end_line, self.file_sha256, self.span_sha256)
        if self.kind in {"source", "diff"}:
            if any(x is None for x in source_fields) or self.report_id is not None or self.report_sha256 is not None:
                raise ProtocolError("invalid_source_evidence")
            relative_path(self.path)
            if self.end_line < self.start_line or (self.kind == "diff") != (self.side == "diff"):
                raise ProtocolError("invalid_source_range_or_side")
        elif any(x is not None for x in source_fields) or self.report_id is None or self.report_sha256 is None:
            raise ProtocolError("invalid_report_evidence")
        return self


class Handoff(Message):
    handoff_id: Identifier
    run_id: Identifier
    from_role_run_id: Identifier
    to_role_run_id: Identifier
    purpose: Literal["review_candidate"] = "review_candidate"
    task_digest: Digest
    baseline_digest: Digest
    candidate_id: Identifier
    source_tree_digest: Digest
    patch_sha256: Digest
    snapshot_id: Identifier
    read_document_ids: Annotated[tuple[Identifier, ...], Field(max_length=4096)]
    report_ids: Annotated[tuple[Identifier, ...], Field(max_length=20)] = ()
    evidence_ids: Annotated[tuple[Identifier, ...], Field(max_length=30)] = ()
    unverified_claims: Annotated[tuple[Text, ...], Field(max_length=10)] = ()
    questions: Annotated[tuple[Text, ...], Field(min_length=1, max_length=10)]
    deadline_seconds: Annotated[int, Field(ge=1, le=1800)] = 120
    write: bool = False
    execute: bool = False


class SuggestedValidation(Message):
    input: Text
    expected: Text


class Finding(Message):
    finding_id: Identifier
    severity: Literal["major", "minor", "informational"]
    category: Literal["behavior", "compatibility", "scope", "validation", "style"]
    claim: Text
    evidence_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=10)]
    requirement_ref: Identifier
    impact: Text
    suggested_validation: SuggestedValidation | None = None
    confidence: Literal["low", "medium", "high"] = "medium"


class ReviewReport(Message):
    review_id: Identifier
    run_id: Identifier
    reviewer_role_run_id: Identifier
    candidate_id: Identifier
    source_tree_digest: Digest
    verdict: Literal["approve", "request_changes", "insufficient_evidence"]
    findings: Annotated[tuple[Finding, ...], Field(max_length=20)] = ()
    coverage: Annotated[tuple[ShortText, ...], Field(min_length=1, max_length=12)]
    unknowns: Annotated[tuple[ShortText, ...], Field(max_length=12)] = ()

    @model_validator(mode="after")
    def verdict_is_consistent(self):
        if len({f.finding_id for f in self.findings}) != len(self.findings):
            raise ProtocolError("duplicate_finding")
        actionable = [f for f in self.findings if f.severity != "informational" and f.category != "style"]
        if self.verdict == "request_changes" and not actionable:
            raise ProtocolError("request_changes_without_actionable_finding")
        if self.verdict == "approve" and any(f.severity == "major" for f in self.findings):
            raise ProtocolError("approve_with_unresolved_major_finding")
        if self.verdict == "insufficient_evidence" and not self.unknowns:
            raise ProtocolError("missing_evidence_gap")
        return self


class FindingResponse(Message):
    response_id: Identifier
    run_id: Identifier
    responder_role_run_id: Identifier
    review_id: Identifier
    finding_id: Identifier
    reviewed_candidate_id: Identifier
    target_candidate_id: Identifier
    disposition: Literal["accepted", "rejected", "needs_evidence"]
    reason: Text
    evidence_ids: Annotated[tuple[Identifier, ...], Field(max_length=10)] = ()


def bind_version(value, candidate: Candidate):
    if value.run_id != candidate.run_id or value.candidate_id != candidate.candidate_id or value.source_tree_digest != candidate.source_tree_digest:
        raise ProtocolError("version_or_run_mismatch")


def validate_evidence(evidence: EvidenceRef, candidate: Candidate, documents, reports):
    bind_version(evidence, candidate)
    if evidence.kind in {"source", "diff"}:
        doc = documents.get(evidence.document_id)
        if doc is None or (evidence.path, evidence.side, evidence.file_sha256) != (doc.path, doc.side, doc.sha256):
            raise ProtocolError("source_reference_mismatch")
        if evidence.end_line > doc.line_count:
            raise ProtocolError("source_range_outside_document")
        # Span bytes are validated when the broker issues/registers evidence.
    else:
        report = reports.get(evidence.report_id)
        if report is None:
            raise ProtocolError("report_not_available")
        bind_version(report, candidate)
        if report.origin != evidence.kind or sha256(canonical_bytes(report)) != evidence.report_sha256:
            raise ProtocolError("report_origin_or_hash_mismatch")


def validate_handoff(handoff: Handoff, candidate: Candidate, receiver: RoleIdentity, reports, evidence):
    bind_version(handoff, candidate)
    expected = (candidate.task_digest, candidate.baseline_digest, candidate.patch_sha256, candidate.snapshot_id, candidate.producer_role_run_id)
    actual = (handoff.task_digest, handoff.baseline_digest, handoff.patch_sha256, handoff.snapshot_id, handoff.from_role_run_id)
    if actual != expected or receiver.run_id != candidate.run_id or handoff.to_role_run_id != receiver.role_run_id:
        raise ProtocolError("handoff_identity_mismatch")
    if len(set(handoff.read_document_ids)) != len(handoff.read_document_ids) or not set(handoff.read_document_ids) <= {doc.document_id for doc in candidate.documents}:
        raise ProtocolError("handoff_document_scope_mismatch")
    if not set(handoff.report_ids) <= reports.keys() or not set(handoff.evidence_ids) <= evidence.keys():
        raise ProtocolError("handoff_reference_not_available")
    for report_id in handoff.report_ids:
        bind_version(reports[report_id], candidate)
    documents = {doc.document_id: doc for doc in candidate.documents}
    for evidence_id in handoff.evidence_ids:
        ref = evidence[evidence_id]
        validate_evidence(ref, candidate, documents, reports)
        if ref.document_id is not None and ref.document_id not in handoff.read_document_ids:
            raise ProtocolError("handoff_evidence_outside_scope")
    return handoff


def validate_review(review: ReviewReport, candidate: Candidate, reviewer: RoleIdentity, evidence, documents, reports, requirements):
    bind_version(review, candidate)
    if reviewer.run_id != candidate.run_id or review.reviewer_role_run_id != reviewer.role_run_id:
        raise ProtocolError("reviewer_identity_mismatch")
    requirement_ids = {r.requirement_id for r in requirements}
    for finding in review.findings:
        if finding.requirement_ref not in requirement_ids:
            raise ProtocolError("requirement_not_registered")
        substantive = False
        for evidence_id in finding.evidence_ids:
            ref = evidence.get(evidence_id)
            if ref is None:
                raise ProtocolError("evidence_not_registered")
            validate_evidence(ref, candidate, documents, reports)
            substantive |= ref.kind != "model_claim"
        if not substantive:
            raise ProtocolError("model_claim_is_not_independent_evidence")
    return review


def validate_response(response: FindingResponse, review: ReviewReport, reviewed: Candidate, target: Candidate, responder: RoleIdentity, evidence, documents, reports):
    bind_version(review, reviewed)
    if (response.run_id, response.responder_role_run_id, response.review_id, response.reviewed_candidate_id, response.target_candidate_id) != (review.run_id, responder.role_run_id, review.review_id, reviewed.candidate_id, target.candidate_id) or responder.run_id != review.run_id:
        raise ProtocolError("response_identity_mismatch")
    if target.run_id != reviewed.run_id or target.task_digest != reviewed.task_digest or target.baseline_digest != reviewed.baseline_digest:
        raise ProtocolError("response_candidate_lineage_mismatch")
    if response.finding_id not in {f.finding_id for f in review.findings}:
        raise ProtocolError("finding_not_available")
    if response.disposition == "rejected" and not response.evidence_ids:
        raise ProtocolError("rejection_needs_evidence")
    substantive = False
    for evidence_id in response.evidence_ids:
        ref = evidence.get(evidence_id)
        if ref is None:
            raise ProtocolError("response_evidence_not_registered")
        validate_evidence(ref, target, documents, reports)
        substantive |= ref.kind != "model_claim"
    if response.disposition == "rejected" and not substantive:
        raise ProtocolError("rejection_needs_independent_evidence")
    # Accepted is a recorded intention, not proof that a defect is resolved.
    return response
