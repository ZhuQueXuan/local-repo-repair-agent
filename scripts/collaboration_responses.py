"""Validate reply metadata before sealing; paths never become evidence IDs."""
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from collaboration_protocol import Identifier, Text, ProtocolError, canonical_bytes, sha256, tree_digest
from review_tools import _read


class InvalidFindingResponses(ProtocolError):
    """Fixed reason code only; never echo arbitrary file content to a model."""


class ResponseDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    finding_id: Identifier
    disposition: Literal["accepted", "rejected", "needs_evidence"]
    reason: Text
    evidence_ids: Annotated[tuple[Identifier, ...], Field(max_length=10)]


def parse_response_drafts(raw, review, evidence, manifest, work):
    if len(raw) > 16384:
        raise InvalidFindingResponses("response_file_too_large")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        raise InvalidFindingResponses("response_file_not_json") from None
    if not isinstance(payload, list) or len(payload) > 20:
        raise InvalidFindingResponses("response_list_required_max_20")
    drafts, seen = [], set()
    available = {f.finding_id for f in review.findings}
    for row in payload:
        try:
            draft = ResponseDraft.model_validate_json(canonical_bytes(row))
        except (ValidationError, ValueError, TypeError):
            raise InvalidFindingResponses("invalid_response_fields_or_evidence_ids") from None
        if draft.finding_id not in available:
            raise InvalidFindingResponses("finding_not_in_current_review")
        if draft.finding_id in seen:
            raise InvalidFindingResponses("duplicate_finding_response")
        seen.add(draft.finding_id)
        if any(e not in evidence for e in draft.evidence_ids):
            raise InvalidFindingResponses("evidence_id_not_registered")
        if draft.disposition == "rejected" and not draft.evidence_ids:
            raise InvalidFindingResponses("rejection_requires_current_registered_evidence")
        drafts.append(draft)
    if any(d.evidence_ids for d in drafts):
        prefix = manifest["source_prefix"].rstrip("/") + "/"
        current = tree_digest({p: sha256(_read(work, p)) for p in manifest["source_files_sha256"] if p.startswith(prefix)})
        for draft in drafts:
            for e in draft.evidence_ids:
                ref = evidence[e]
                if ref.candidate_id != review.candidate_id or ref.source_tree_digest != current:
                    raise InvalidFindingResponses("evidence_not_bound_to_current_source")
    return tuple(drafts)


def response_correction(error_code, review):
    return ("Your submit command completed, but this phase is NOT accepted yet: response metadata failed validation (" + error_code + "). "
        "Continue this SAME revision phase within its original deadline. Correct temp/finding_responses.json, then submit again. "
        "Each list entry must have exactly finding_id, disposition (accepted/rejected/needs_evidence), reason (1..2000 characters), evidence_ids (a list). "
        "evidence_ids contains ONLY existing registered identifiers that bind the current source version. NEVER put file paths, commands or tool-output paths there. "
        "For accepted or needs_evidence, [] is allowed; mention a local test-output path in the brief reason if useful, without claiming it is registered evidence. "
        "For rejected, registered evidence is required and old-version evidence cannot justify a changed target. A defect you fixed was accepted, not refuted. "
        "Missing replies remain unanswered; do not invent replies or evidence. Re-run checks if you change implementation. "
        "When ready execute echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT by itself. Allowed finding IDs: " +
        canonical_bytes([f.finding_id for f in review.findings]).decode())
