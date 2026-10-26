"""Append-only program adjudication sidecars, separate from repair outcomes.

No model/HTTP write capability. Only the fixed trusted diagnostic probe runs.
Exact finding mappings are developer-reviewed and versioned; unknowns stay so.
"""
import argparse
import json
import re
from pathlib import Path
import sqlite3
import time
import uuid
import zipfile

from collaboration_protocol import canonical_bytes, sha256, ProtocolError
from review_adjudication import (OfflineAdjudicator, ProbePlan, FindingBinding,
                                  FindingDecision, decision_summary)
from review_tools import _read, _root, _unlinked
from task_service_store import ROOT, TERMINAL, worker_lock, now

REGISTRY = ROOT / "configs/adjudication_bindings_v1.json"
PROBE = ROOT / "scripts/adjudication_checks.py"
CHECK_IDS = ("zero", "none", "false", "zeros", "nonempty", "empty_tail")


def ensure_schema(store):
    with worker_lock(store.home, "adjudication_migration.lock") as acquired:
        if not acquired:
            raise ProtocolError("adjudication_migration_busy")
        with store.connect() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='collaboration_adjudications'").fetchone():
                return
            with sqlite3.connect(store.home / ("before_adjudication_" + uuid.uuid4().hex + ".sqlite3")) as backup:
                db.backup(backup)
            db.execute("""CREATE TABLE collaboration_adjudications (
                run_id TEXT NOT NULL, batch_id TEXT NOT NULL, input_digest TEXT NOT NULL,
                created TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
                PRIMARY KEY(run_id,batch_id), UNIQUE(run_id,input_digest))""")


def _stored(store, run_id, input_digest=None):
    with store.connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='collaboration_adjudications'").fetchone()
        if not exists:
            return []
        query, args = "SELECT payload,digest FROM collaboration_adjudications WHERE run_id=?", [run_id]
        if input_digest:
            query += " AND input_digest=?"
            args.append(input_digest)
        return db.execute(query + " ORDER BY rowid LIMIT 21", args).fetchall()


def read_batches(store, run_id):
    """Read-only integrity checked projection; never launches a check process."""
    rows = _stored(store, run_id)
    if len(rows) > 20:
        raise ProtocolError("adjudication_batch_preview_bound")
    result = []
    for row in rows:
        if sha256(row["payload"].encode()) != row["digest"]:
            raise ProtocolError("adjudication_row_changed")
        meta = json.loads(row["payload"])
        if not re.fullmatch(r"batch-[0-9a-f]{32}", meta["batch_id"]):
            raise ProtocolError("invalid_adjudication_batch_id")
        directory = store.home / "adjudications" / meta["batch_id"]
        raw = _read(directory, "result.json")
        if sha256(raw) != meta["result_sha256"]:
            raise ProtocolError("adjudication_result_changed")
        bundle = json.loads(raw)
        if bundle["run_id"] != run_id or bundle["input_digest"] != meta["input_digest"]:
            raise ProtocolError("adjudication_bundle_identity_mismatch")
        for d in bundle["decisions"]:
            decision = FindingDecision.parse(d)
            if decision.run_id != run_id:
                raise ProtocolError("adjudication_decision_run_mismatch")
            review = store.records(run_id, "review").get(decision.review_id)
            finding = next((f for f in review.findings if f.finding_id == decision.finding_id), None) if review else None
            if finding is None or sha256(canonical_bytes(finding)) != decision.finding_sha256:
                raise ProtocolError("adjudication_original_finding_changed")
            for candidate_id, digest in ((decision.reviewed_candidate_id, decision.reviewed_source_tree_digest),
                                         (decision.target_candidate_id, decision.target_source_tree_digest)):
                if store.snapshot(run_id, candidate_id).candidate.source_tree_digest != digest:
                    raise ProtocolError("adjudication_candidate_changed")
        result.append({**bundle, "batch_id": meta["batch_id"], "created": meta["created"]})
    return result


def _registry():
    value = json.loads(_read(REGISTRY.parent, REGISTRY.name))
    if set(value) != {"schema_version", "scope", "bindings"} or value["schema_version"] != 1 or not isinstance(value["bindings"], list):
        raise ProtocolError("invalid_trusted_binding_registry")
    return value


def _publish(store, run_id, input_digest, result, scratch):
    """Trusted publisher, never exposed as a model or web mutation endpoint."""
    batch_id = "batch-" + input_digest[:32]
    base = store.home / "adjudications"
    base.mkdir(exist_ok=True)
    _root(base)
    directory = base / batch_id
    directory.mkdir()
    raw = canonical_bytes(result)
    if len(raw) > 1024 * 1024:
        raise ProtocolError("adjudication_result_too_large")
    from dotenv import dotenv_values
    secrets = [v.encode() for k, v in dotenv_values(ROOT / ".env", interpolate=False).items() if "KEY" in k and v]
    def safe(data):
        if any(secret in data for secret in secrets):
            raise ProtocolError("credential_in_adjudication_output")
        return data
    (directory / "result.json").write_bytes(safe(raw))
    hashes = {}
    with zipfile.ZipFile(directory / "proof.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for path in scratch.rglob("*"):
            _unlinked(path)
            if path.is_file():
                data = safe(path.read_bytes())
                relative = path.relative_to(scratch).as_posix()
                archive.writestr(relative, data)
                hashes[relative] = sha256(data)
    (directory / "proof-files.json").write_bytes(canonical_bytes(hashes))
    manifest = {p.name: sha256(p.read_bytes()) for p in directory.iterdir() if p.is_file()}
    (directory / "artifact_sha256.json").write_bytes(canonical_bytes(manifest))
    meta = {"batch_id": batch_id, "input_digest": input_digest, "created": now(), "result_sha256": sha256(raw),
            "artifact_manifest_sha256": sha256((directory / "artifact_sha256.json").read_bytes())}
    payload = canonical_bytes(meta).decode()
    with store.transaction() as db:
        db.execute("INSERT INTO collaboration_adjudications VALUES(?,?,?,?,?,?)",
            (run_id, batch_id, input_digest, meta["created"], payload, sha256(payload.encode())))
    return batch_id


def adjudicate_run(store, run_id, *, timeout=60):
    """Fixed diagnostics only; supplemental post-run checks never change scores."""
    row = store.get(run_id)
    if row["status"] not in TERMINAL:
        raise ProtocolError("adjudication_requires_terminal_run")
    if row["kind"] != "collaboration_live" or row["task"] != "split-after-boundary" or row.get("collaboration_policy", {}).get("diagnostic_profile") != "seeded_falsey_remainder_v1":
        return None
    if not 0 < timeout <= 120:
        raise ValueError("Use bounded supplemental evaluation time")
    reviews = store.records(run_id, "review")
    if not reviews or not row.get("final_candidate_id"):
        return None
    target = store.snapshot(run_id, row["final_candidate_id"])
    # The publisher holds its own lock and never re-dispatches a model/run.
    with worker_lock(store.home, "adjudication_worker.lock") as acquired:
        if not acquired:
            raise ProtocolError("adjudication_already_running")
        frozen = {name: sha256((ROOT / name).read_bytes()) for name in
            ("scripts/review_adjudication.py", "scripts/collaboration_adjudication.py", "scripts/adjudication_checks.py", "configs/adjudication_bindings_v1.json")}
        registry = _registry()
        if sha256(REGISTRY.read_bytes()) != frozen["configs/adjudication_bindings_v1.json"]:
            raise ProtocolError("adjudication_registry_changed")
        identity = {"run_id": run_id, "task_digest": row["task_digest"], "target": target.candidate.model_dump(mode="json"),
            "reviews": [r.model_dump(mode="json") for r in reviews.values()], "evaluator_inputs": frozen}
        input_digest = sha256(canonical_bytes(identity))
        if _stored(store, run_id, input_digest):
            return next(v for v in read_batches(store, run_id) if v["input_digest"] == input_digest)
        scratch = ROOT / "temp/adjudication" / (run_id[:12] + "-" + uuid.uuid4().hex[:12])
        judge = OfflineAdjudicator(scratch)
        (scratch / "evaluation-inputs.json").write_bytes(canonical_bytes(identity))
        (scratch / "trusted-registry.json").write_bytes(canonical_bytes(registry))
        end = time.monotonic() + timeout
        def check():
            current = store.get(run_id)
            if current["cancel_requested"] or current["status"] == "cancelled":
                raise ProtocolError("adjudication_parent_cancelled")
            if time.monotonic() >= end:
                raise TimeoutError("Supplemental adjudication deadline")
        plan = ProbePlan.parse({"plan_id": "falsey-diagnostic-v1", "task_digest": row["task_digest"], "probe_sha256": frozen["scripts/adjudication_checks.py"],
            "scope": "Six already-disclosed maxsplit=1 cases; no hidden evaluation or general behavior proof.",
            "checks": [{"check_id": k, "test_id": "registered_probe.FalseyRemainder.test_" + k,
                        "requirement_ref": "issue:paragraph-4", "prior_visibility": "already_reported"} for k in CHECK_IDS]})
        key = judge.register_probe(plan, PROBE)
        decisions, summaries = [], []
        after = judge.run_probe(target, key, timeout=min(10, max(.01, end - time.monotonic())), check=check)
        evidence, reports = store.records(run_id, "evidence"), store.records(run_id, "report")
        for review in reviews.values():
            check()
            before = store.snapshot(run_id, review.candidate_id)
            role = store.role(review.reviewer_role_run_id)
            from collaboration import role_identity
            receipt = judge.run_probe(before, key, timeout=min(10, max(.01, end - time.monotonic())), check=check)
            bindings = []
            for item in registry["bindings"]:
                if item["run_id"] == run_id and item["binding"]["review_id"] == review.review_id:
                    bindings.append(FindingBinding.parse(item["binding"]))
            outcome = judge.adjudicate(review, role_identity(role), before, target, evidence=evidence, reports=reports,
                plan_key=key, reviewed_receipt_id=receipt.receipt_id, target_receipt_id=after.receipt_id, bindings=bindings)
            decisions.extend(d.model_dump(mode="json") for d in outcome)
            summaries.append({"review_id": review.review_id, "reviewed_candidate_id": review.candidate_id,
                              "target_candidate_id": target.candidate.candidate_id, "summary": decision_summary(outcome)})
        check()
        if any(sha256((ROOT / p).read_bytes()) != h for p, h in frozen.items()):
            raise ProtocolError("adjudication_inputs_changed")
        result = {"schema_version": 1, "run_id": run_id, "input_digest": input_digest, "status": "completed",
            "method": "trusted_frozen_checks_and_exact_finding_bindings", "plan": plan.model_dump(mode="json"),
            "plan_digest": key, "evaluator_inputs": frozen, "decisions": decisions, "summaries": summaries,
            "receipts": [r.model_dump(mode="json") | {"results": results} for r, _, _, results in judge._receipts.values()],
            "scope": "Supplemental declared-case adjudication. Does not alter repair outcome, review verdict or fees; unmapped claims remain unresolved. All six checks were disclosed already; zero autonomous-new-discovery claim.",
            "evaluation_seconds": round(timeout - max(0, end - time.monotonic()), 3)}
        _publish(store, run_id, input_digest, result, scratch)
        return next(v for v in read_batches(store, run_id) if v["input_digest"] == input_digest)


def read_proof(store, run_id, batch_id, name):
    if name not in {"result.json", "proof.zip", "proof-files.json", "artifact_sha256.json"}:
        raise ProtocolError("adjudication_file_not_registered")
    rows = read_batches(store, run_id)
    if not any(v["batch_id"] == batch_id for v in rows):
        raise ProtocolError("adjudication_batch_not_for_run")
    directory = store.home / "adjudications" / batch_id
    entry = next(json.loads(row["payload"]) for row in _stored(store, run_id) if json.loads(row["payload"])["batch_id"] == batch_id)
    manifest_raw = _read(directory, "artifact_sha256.json")
    if sha256(manifest_raw) != entry["artifact_manifest_sha256"]:
        raise ProtocolError("adjudication_manifest_changed")
    manifest = json.loads(manifest_raw)
    from collaboration_panel import checked_bytes
    data = checked_bytes(directory, name)
    if name != "artifact_sha256.json" and sha256(data) != manifest.get(name):
        raise ProtocolError("adjudication_proof_hash_mismatch")
    return data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--home", type=Path, default=ROOT / "data/task_service")
    parser.add_argument("--panel-store", action="store_true")
    args = parser.parse_args()
    from collaboration_store import CollaborationStore
    store = CollaborationStore(args.home, allow_production=args.panel_store)
    batch = adjudicate_run(store, args.run_id)
    print(json.dumps({"run_id": args.run_id, "batch_id": batch["batch_id"] if batch else None,
                      "status": batch["status"] if batch else "not_applicable"}, ensure_ascii=False))
