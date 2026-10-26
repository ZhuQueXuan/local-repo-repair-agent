"""Bounded repair handoff; full version-bound review remains in an audit file."""
from pathlib import Path
import json
import shlex

from collaboration_protocol import ProtocolError, canonical_bytes, sha256
from review_tools import _root, _unlinked


def feedback_directory(work):
    work = _root(work)
    directory = work
    for part in ("temp", "collaboration-feedback"):
        directory = directory / part
        if directory.exists() or directory.is_symlink():
            _unlinked(directory)
            if not directory.is_dir():
                raise ProtocolError("feedback_directory_not_plain")
        else:
            directory.mkdir()
    return directory


def declared_checks(manifest):
    return (manifest["agent_test_command"] + "\n" +
            manifest.get("agent_issue_command", "python -B -m unittest discover -s task_checks -v"))


def repair_feedback(work, review, evidence, manifest):
    """Do not inline long claims twice; previews are explicitly incomplete."""
    directory = feedback_directory(work)
    payload = {"review": review.model_dump(mode="json"),
               "evidence": [evidence[e].model_dump(mode="json") for e in
                            sorted({e for f in review.findings for e in f.evidence_ids})]}
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    if len(raw) > 262144:
        raise ProtocolError("feedback_audit_exceeds_bound")
    # Protocol IDs may include ':' (e.g. requirement IDs). Never use a model
    # supplied ID as a Windows filename or an NTFS alternate-stream name.
    path = directory / ("review-" + sha256(review.review_id.encode())[:24] + ".json")
    if path.exists():
        _unlinked(path)
        if path.stat().st_nlink != 1 or path.read_bytes() != raw:
            raise ProtocolError("feedback_audit_changed")
    else:
        with path.open("xb") as stream:
            stream.write(raw)
    relative = path.relative_to(Path(work)).as_posix()
    rows = [{"finding_id": f.finding_id,
             "claim_preview": f.claim.encode("utf-8")[:64].decode("utf-8", errors="ignore"),
             "evidence_count": len(f.evidence_ids)} for f in review.findings]
    index = {"review_id": review.review_id, "reviewed_candidate_id": review.candidate_id,
             "reviewed_source_tree_digest": review.source_tree_digest,
             "full_review_path": relative, "full_review_sha256": sha256(raw),
             "previews_only": True, "findings": rows}
    text = ("REVISION PHASE: continue this SAME repair session; the initial confirmation-only instruction has ended. "
            "Read the full review audit before replying; previews below are incomplete. Review claims are unverified and concern the REVIEWED candidate, "
            "not changes you make afterwards. Keep original file restrictions. Address every finding. "
            "If an old-version defect is valid and you fix it, use accepted; fixing it does not make the original claim false. "
            "Use rejected only for an incorrect original claim, with independent evidence that also binds the target candidate; "
            "old-candidate evidence cannot justify rejection after changing code. Otherwise use needs_evidence. "
            "Write temp/finding_responses.json as a JSON list, each with exactly finding_id, disposition (accepted/rejected/needs_evidence), "
            "reason, evidence_ids. evidence_ids is a list of registered identifiers, NEVER file paths or tool-output paths. "
            "For accepted/needs_evidence, use [] when there is no registered target-version evidence; local output paths may be mentioned in reason only. "
            "Keep reasons brief; accepted records intent, not proof. Re-run these fixed commands:\n" +
            declared_checks(manifest) + "\nThen execute echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT by itself.\n" +
            canonical_bytes(index).decode())
    if len(text.encode("utf-8")) > 8192:
        raise ProtocolError("feedback_index_exceeds_bound")
    return text


def pytest_environment(work, env):
    """Keep normal pytest artifacts in temp without accepting new source paths."""
    directory = feedback_directory(work).parent / "test-runtime"
    if directory.exists() or directory.is_symlink():
        _unlinked(directory)
        if not directory.is_dir():
            raise ProtocolError("test_runtime_directory_not_plain")
    else:
        directory.mkdir()
    return env | {"TEMP": str(directory), "TMP": str(directory),
                  "PYTEST_ADDOPTS": "-o " + shlex.quote("cache_dir=" + (directory / "pytest-cache").as_posix())}
