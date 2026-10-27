"""Additive, temp-only stage-B storage over the existing RunStore.

No production database is migrated by importing this module. Only trusted
program code can register outputs; these methods are not model/HTTP tools.
"""
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import sqlite3
import uuid

from collaboration_protocol import (Candidate, EvidenceRef, FindingResponse, Handoff,
    ProgramReport, ProtocolError, Requirement, ReviewReport, RoleIdentity,
    canonical_bytes, sha256, validate_evidence, validate_handoff, validate_response, validate_review)
from review_tools import ReviewSnapshot, IntegrityError
from task_service_store import ROOT, DEFAULT_HOME, RunStore, TERMINAL, catalog, now, policy, worker_lock
from task_workspace import load_task
from document_agent import ConsultationRecord, validate_document_policy, validate_consultation

PHASES = ("repairing", "reviewing", "revising", "final_review")
PHASE_ORDER = ("consulting",) + PHASES
TYPES = {"consultation": ConsultationRecord, "evidence": EvidenceRef, "report": ProgramReport, "handoff": Handoff,
         "review": ReviewReport, "response": FindingResponse}
KEYS = {"consultation": "advice_id", "evidence": "evidence_id", "report": "report_id", "handoff": "handoff_id",
        "review": "review_id", "response": "response_id"}


class StateConflict(ProtocolError):
    pass


class CollaborationStopped(ProtocolError):
    pass


class CollaborationStore(RunStore):
    def __init__(self, home, *, allow_production=False):
        home = Path(home).resolve()
        if not home.is_relative_to((ROOT / "temp").resolve()) and not (allow_production and home == DEFAULT_HOME.resolve()):
            raise ValueError("Default temp-only storage; explicitly enable only the standard panel database.")
        super().__init__(home)
        with worker_lock(home, "collaboration_migration.lock") as acquired:
            if not acquired:
                raise StateConflict("collaboration_migration_already_owned")
            self._migrate()
        from collaboration_adjudication import ensure_schema
        ensure_schema(self)

    def _migrate(self):
        with self.connect() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collaboration_schema'").fetchone()
            if exists:
                versions = [row[0] for row in db.execute("SELECT version FROM collaboration_schema")]
                if versions != [1]:
                    raise StateConflict("unsupported_collaboration_schema")
                required = {"collaboration_runs", "role_runs", "collaboration_objects", "logical_requests", "request_attempts"}
                present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not required <= present:
                    raise StateConflict("incomplete_collaboration_schema")
                return
            # sqlite backup is consistent even if the old store used journaling.
            backup = self.home / ("before_collaboration_" + uuid.uuid4().hex + ".sqlite3")
            with sqlite3.connect(backup) as target:
                db.backup(target)
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE collaboration_schema(version INTEGER PRIMARY KEY);
                INSERT INTO collaboration_schema VALUES(1);
                CREATE TABLE collaboration_runs (
                    run_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, phase TEXT NOT NULL,
                    active_role TEXT, candidate_id TEXT, review_id TEXT, policy_digest TEXT NOT NULL);
                CREATE TABLE role_runs (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, phase TEXT NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(run_id,phase));
                CREATE UNIQUE INDEX one_running_role ON role_runs(run_id) WHERE status='running';
                CREATE TABLE collaboration_objects (
                    run_id TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
                    role_id TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(run_id,kind,id));
                CREATE TABLE logical_requests (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, role_id TEXT NOT NULL,
                    request_key TEXT NOT NULL, input_digest TEXT NOT NULL, state TEXT NOT NULL,
                    payload TEXT NOT NULL, UNIQUE(role_id,request_key));
                CREATE TABLE request_attempts (
                    ticket TEXT PRIMARY KEY, request_id TEXT NOT NULL, provider TEXT NOT NULL,
                    state TEXT NOT NULL, UNIQUE(request_id,provider));
                COMMIT;
            """)

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def _load(self, db, run_id):
        row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
        meta = db.execute("SELECT * FROM collaboration_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None or meta is None:
            raise KeyError("Collaboration run not found")
        return json.loads(row[0]), dict(meta)

    def _parent(self, db, run_id, **fields):
        parent, meta = self._load(db, run_id)
        parent.update(fields, updated=now())
        db.execute("UPDATE runs SET status=?,payload=? WHERE id=?", (parent["status"], canonical_bytes(parent).decode(), run_id))
        return parent

    def _active(self, db, role_id, expected_revision=None):
        row = db.execute("SELECT payload FROM role_runs WHERE id=?", (role_id,)).fetchone()
        if row is None:
            raise KeyError("Role not found")
        role = json.loads(row[0])
        parent, meta = self._load(db, role["run_id"])
        if parent["status"] != "running" or parent["cancel_requested"]:
            raise CollaborationStopped("parent_cancelled_or_not_running")
        if role["status"] != "running" or meta["active_role"] != role_id:
            raise StateConflict("role_not_active")
        if expected_revision is not None and meta["revision"] != expected_revision:
            raise StateConflict("stale_parent_revision")
        return role, parent, meta

    def create_collaboration(self, task, profile, request_key, collaboration_policy, *, input_manifest=None, input_issue=None, execution="mock", task_card=None, allow_pause=False, allow_capabilities=False, model_profile=None, _defer_dispatch=False):
        if execution not in {"mock", "live"} or execution == "live" and task == "synthetic-arithmetic":
            raise ValueError("Live collaboration requires a fixed catalog task")
        kind = "collaboration_mock" if execution == "mock" else "collaboration_live"
        tasks = {item["id"]: item for item in catalog()}
        configuration = policy(profile)
        if task not in tasks and task != "synthetic-arithmetic" or not isinstance(request_key, str) or not 1 <= len(request_key) <= 120:
            raise ValueError("Select a fixed task and bounded idempotency key")
        if task == "synthetic-arithmetic":
            if not isinstance(input_manifest, dict) or not isinstance(input_issue, bytes):
                raise ValueError("Synthetic tasks require explicit trusted fixture inputs")
            scope = "Synthetic arithmetic fixture; not a catalog repair or model evaluation."
        else:
            if input_manifest is not None or input_issue is not None:
                raise ValueError("Catalog tasks use their frozen inputs")
            source, input_manifest = load_task(task)
            input_issue = (source / "issue.md").read_bytes()
            scope = tasks[task]["scope"]
        task_digest = sha256(canonical_bytes({"manifest": input_manifest, "issue_sha256": sha256(input_issue)}))
        # A bounded public policy object, not executable code or private settings.
        if not isinstance(collaboration_policy, dict) or len(canonical_bytes(collaboration_policy)) > 8192:
            raise ValueError("Invalid collaboration policy")
        from collaboration_diagnostic import validate_policy, SCOPE
        diagnostic = validate_policy(collaboration_policy, task, profile, execution)
        from self_review import validate_self_policy, SCOPE as SELF_SCOPE
        self_review = validate_self_policy(collaboration_policy, profile)
        documents = validate_document_policy(collaboration_policy, profile)
        if type(allow_pause) is not bool or type(_defer_dispatch) is not bool:
            raise ValueError("Invalid checkpoint options")
        if allow_pause and (task_card is None or diagnostic or self_review or execution != "live"):
            raise ValueError("暂停仅用于开启任务卡的新建双/三角色任务，自查和诊断暂不支持。")
        if type(allow_capabilities) is not bool or allow_capabilities and (diagnostic or self_review or execution != "live"):
            raise ValueError("工具与Skills扩展仅用于新建普通双/三角色任务，自查和诊断暂不支持。")
        if documents and (diagnostic or self_review):
            raise ValueError("Choose either document consultation or diagnostic/self-check")
        phases = PHASE_ORDER if documents else PHASES
        from model_profiles import selection_fields
        model_fields = selection_fields(model_profile,execution=execution,phases=phases)
        if diagnostic:
            scope = SCOPE
        if self_review:
            scope = SELF_SCOPE + " " + scope
        if documents:
            from document_agent import SCOPE as DOCUMENT_SCOPE
            scope = DOCUMENT_SCOPE + " " + scope
        collaboration_policy = {"parent_seconds": 1800,
            "role_seconds": {"repairing": 300, "reviewing": 120, "revising": 180, "final_review": 90}, **collaboration_policy}
        limits = collaboration_policy["role_seconds"]
        if not isinstance(limits, dict) or set(limits) != set(phases):
            raise ValueError("Declare all role deadlines")
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 or v > 3600 for v in [collaboration_policy["parent_seconds"], *limits.values()]):
            raise ValueError("Invalid bounded runtime deadline")
        digest = sha256(canonical_bytes(collaboration_policy))
        from task_cards import validate_card, ensure_schema, creation_fields, insert_card
        card = validate_card(task_card) if task_card is not None else None
        if card is not None:
            ensure_schema(self)
        with self.transaction() as db:
            found = db.execute("SELECT id,payload FROM runs WHERE request_key=?", (request_key,)).fetchone()
            if found:
                old = json.loads(found["payload"])
                if (old.get("task"), old.get("profile"), old.get("kind"), old.get("policy_digest"), old.get("task_digest"), old.get("task_card_seed_sha256"), old.get("harness_pause_version") == 1, old.get("harness_capabilities_version") == 1,old.get('model_profile_sha256')) != (task, profile, kind, digest, task_digest, creation_fields(card).get("task_card_seed_sha256"), allow_pause, allow_capabilities,model_fields.get('model_profile_sha256')):
                    raise StateConflict("idempotency_key_content_conflict")
                return old
            run_id, timestamp = uuid.uuid4().hex, now()
            record = {"id": run_id, "kind": kind, "task": task, "profile": profile,
                      "status": "queued", "stage": phases[0], "created": timestamp, "updated": timestamp,
                      "cancel_requested": False, "agent_exit": None, "repair_result": "not_run", "metrics": {},
                      "artifacts": {}, "scope": scope, "configuration": configuration, "task_digest": task_digest,
                      "collaboration_mode": "document_review_" + execution if documents else "self_review_" + execution if self_review else "dual_review_diagnostic" if diagnostic else "dual_review_" + execution, "collaboration_policy": collaboration_policy,
                      "policy_digest": digest, "collaboration_result": "not_completed", "review_verdict": "not_run"}
            record.update(creation_fields(card))
            record.update(model_fields)
            if allow_pause:
                record.update(harness_pause_version=1, pause_requested=False)
            if allow_capabilities:
                record["harness_capabilities_version"] = 1
            if _defer_dispatch:
                record["status"] = "preparing_continuation"
            if card is not None:
                record["scope"] += " Interactive human task card enabled; not a frozen baseline comparison."
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?)", (run_id, request_key, record["status"], timestamp, canonical_bytes(record).decode()))
            if card is not None:
                insert_card(self, db, run_id, card)
            db.execute("INSERT INTO collaboration_runs VALUES(?,0,?,NULL,NULL,NULL,?)", (run_id, phases[0], digest))
            sessions = {"repairer": uuid.uuid4().hex, "reviewing": uuid.uuid4().hex, "final_review": uuid.uuid4().hex, "consulting": uuid.uuid4().hex}
            if self_review:
                sessions = dict.fromkeys(sessions, sessions["repairer"])
            prior = None
            for phase in phases:
                identity = RoleIdentity(run_id=run_id, role_run_id=uuid.uuid4().hex,
                    session_id=sessions["repairer"] if phase in {"repairing", "revising"} else sessions[phase],
                    role="document_reader" if phase == "consulting" else "repairer" if phase in {"repairing", "revising"} else "reviewer")
                role = {**identity.model_dump(mode="json"), "id": identity.role_run_id, "phase": phase, "status": "pending",
                        "parent_role_run_id": prior, "created": timestamp, "input_digest": None, "output_digest": None}
                prior = identity.role_run_id
                db.execute("INSERT INTO role_runs VALUES(?,?,?,?,?)", (identity.role_run_id, run_id, phase, "pending", canonical_bytes(role).decode()))
            self.emit(db, run_id, "collaboration_created", {"policy_digest": digest, "mode": record["collaboration_mode"]})
            return record

    def state(self, run_id):
        with self.connect() as db:
            parent, meta = self._load(db, run_id)
        return parent | {"collaboration_state": meta}

    def roles(self, run_id):
        with self.connect() as db:
            roles = [json.loads(row[0]) for row in db.execute("SELECT payload FROM role_runs WHERE run_id=? ORDER BY rowid", (run_id,))]
        return sorted(roles, key=lambda r: PHASE_ORDER.index(r["phase"]))

    def role(self, role_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM role_runs WHERE id=?", (role_id,)).fetchone()
        if row is None:
            raise KeyError("Role not found")
        return json.loads(row[0])

    def claim_parent(self, run_id):
        with self.transaction() as db:
            parent, meta = self._load(db, run_id)
            if parent["status"] != "queued" or parent["cancel_requested"]:
                return False
            self._parent(db, run_id, status="running", started=now())
            self.emit(db, run_id, "collaboration_claimed", {})
            return True

    def claim_role(self, run_id, phase, input_digest, expected_revision, *, wall_limit_seconds=None):
        if len(input_digest) != 64 or any(c not in "0123456789abcdef" for c in input_digest):
            raise ValueError("Invalid role input digest")
        with self.transaction() as db:
            parent, meta = self._load(db, run_id)
            if parent["status"] != "running" or parent["cancel_requested"]:
                raise CollaborationStopped("parent_cancelled_or_not_running")
            if meta["revision"] != expected_revision or meta["phase"] != phase or meta["active_role"]:
                raise StateConflict("stale_or_duplicate_role_claim")
            row = db.execute("SELECT payload FROM role_runs WHERE run_id=? AND phase=?", (run_id, phase)).fetchone()
            if row is None:
                raise StateConflict("unknown_role_phase")
            role = json.loads(row[0])
            if role["status"] != "pending":
                raise StateConflict("role_already_claimed")
            role.update(status="running", started=now(), input_digest=input_digest, wall_limit_seconds=wall_limit_seconds)
            db.execute("UPDATE role_runs SET status='running',payload=? WHERE id=?", (canonical_bytes(role).decode(), role["id"]))
            db.execute("UPDATE collaboration_runs SET active_role=?,revision=revision+1 WHERE run_id=?", (role["id"], run_id))
            self.emit(db, run_id, "role_claimed", {"role_run_id": role["id"], "phase": phase}, role["role"])
            return role

    def _objects(self, db, run_id, kind):
        result = {}
        for row in db.execute("SELECT id,payload,digest FROM collaboration_objects WHERE run_id=? AND kind=?", (run_id, kind)):
            if sha256(row["payload"].encode()) != row["digest"]:
                raise IntegrityError("stored_record_hash_mismatch")
            obj = TYPES[kind].parse(json.loads(row["payload"]))
            if kind == "consultation":
                validate_consultation(obj, self.home)
            result[row["id"]] = obj
        return result

    def records(self, run_id, kind):
        with self.connect() as db:
            return self._objects(db, run_id, kind)

    def snapshot(self, run_id, candidate_id, db=None):
        if db is None:
            with self.connect() as conn:
                return self.snapshot(run_id, candidate_id, conn)
        row = db.execute("SELECT payload,digest FROM collaboration_objects WHERE run_id=? AND kind='snapshot' AND id=?", (run_id, candidate_id)).fetchone()
        if row is None:
            raise ProtocolError("candidate_not_registered")
        if sha256(row["payload"].encode()) != row["digest"]:
            raise IntegrityError("stored_snapshot_hash_mismatch")
        payload = json.loads(row["payload"])
        candidate = Candidate.parse(payload["candidate"])
        snapshot = ReviewSnapshot(self.home / payload["relative_directory"], candidate, payload["manifest_sha256"],
                                  tuple(Requirement.parse(r) for r in payload["requirements"]))
        snapshot.verify()
        return snapshot

    def register_snapshot(self, role_id, snapshot):
        role = self.role(role_id)
        with self.transaction() as db:
            active_role, parent, meta = self._active(db, role_id)
            if snapshot.candidate.task_digest != parent["task_digest"]:
                raise ProtocolError("candidate_parent_input_mismatch")
        c = snapshot.candidate
        if (c.run_id, c.producer_role_run_id) != (role["run_id"], role_id) or role["role"] != "repairer":
            raise ProtocolError("candidate_producer_mismatch")
        snapshot.verify()
        directory = self.home / "snapshots" / sha256(c.candidate_id.encode())
        if not directory.exists():
            directory.mkdir(parents=True)
            (directory / "objects").mkdir()
            for doc in c.documents:
                with (directory / "objects" / doc.document_id).open("xb") as stream:
                    stream.write(snapshot.read(doc.document_id))
                    stream.flush()
                    os.fsync(stream.fileno())
            with (directory / "candidate.json").open("xb") as stream:
                stream.write(canonical_bytes(c))
                stream.flush()
                os.fsync(stream.fileno())
        payload = {"candidate": c.model_dump(mode="json"), "relative_directory": directory.relative_to(self.home).as_posix(),
                   "manifest_sha256": snapshot.manifest_sha256, "requirements": [r.model_dump(mode="json") for r in snapshot.requirements]}
        # Verify before registering. A crash here leaves an unregistered orphan,
        # which is neither exposed nor used to replay work after restart.
        ReviewSnapshot(directory, c, snapshot.manifest_sha256, snapshot.requirements).verify()
        with self.transaction() as db:
            role, parent, meta = self._active(db, role_id)
            if meta["candidate_id"]:
                previous = self.snapshot(c.run_id, meta["candidate_id"], db).candidate
                if (c.task_digest, c.baseline_digest) != (previous.task_digest, previous.baseline_digest):
                    raise ProtocolError("candidate_lineage_mismatch")
            self._put(db, role, "snapshot", c.candidate_id, payload)

    def _put(self, db, role, kind, object_id, payload):
        raw = canonical_bytes(payload)
        old = db.execute("SELECT digest,role_id FROM collaboration_objects WHERE run_id=? AND kind=? AND id=?", (role["run_id"], kind, object_id)).fetchone()
        if old:
            if (old["digest"], old["role_id"]) != (sha256(raw), role["id"]):
                raise StateConflict("immutable_object_content_conflict")
            return
        db.execute("INSERT INTO collaboration_objects VALUES(?,?,?,?,?,?)", (role["run_id"], kind, object_id, role["id"], sha256(raw), raw.decode()))
        self.emit(db, role["run_id"], "collaboration_object_registered", {"kind": kind, "id": object_id, "role_run_id": role["id"], "sha256": sha256(raw)}, role["role"])

    def register_record(self, role_id, kind, payload):
        if kind not in TYPES:
            raise ValueError("Unknown record type")
        obj = TYPES[kind].parse(payload)
        with self.transaction() as db:
            role, parent, meta = self._active(db, role_id)
            if obj.run_id != role["run_id"]:
                raise ProtocolError("record_run_mismatch")
            if kind == "consultation":
                if role["phase"] != "consulting" or role["role"] != "document_reader" or obj.grant.role_run_id != role_id:
                    raise ProtocolError("consultation_producer_mismatch")
                if (obj.grant.task_digest, obj.grant.question) != (parent["task_digest"], parent["collaboration_policy"]["document_question"]):
                    raise ProtocolError("consultation_parent_input_mismatch")
                if obj.relative_directory != "document_snapshots/" + role["run_id"] + "/" + role_id:
                    raise ProtocolError("consultation_directory_mismatch")
                validate_consultation(obj, self.home)
                if self._objects(db, role["run_id"], "consultation"):
                    raise StateConflict("consultation_already_registered")
                self._put(db, role, kind, obj.advice_id, obj.model_dump(mode="json"))
                return obj
            candidate_id = obj.reviewed_candidate_id if kind == "response" else obj.candidate_id
            snapshot = self.snapshot(role["run_id"], candidate_id, db)
            c = snapshot.candidate
            reports, evidence = self._objects(db, c.run_id, "report"), self._objects(db, c.run_id, "evidence")
            docs = {d.document_id: d for d in c.documents}
            identity = RoleIdentity.parse({k: role[k] for k in ("run_id", "role_run_id", "session_id", "role")})
            if kind == "report":
                if (obj.candidate_id, obj.source_tree_digest) != (c.candidate_id, c.source_tree_digest):
                    raise ProtocolError("report_version_mismatch")
                # Only this trusted program method can register program reports.
            elif kind == "evidence":
                if obj.producer_role_run_id != role_id:
                    raise ProtocolError("evidence_producer_mismatch")
                validate_evidence(obj, c, docs, reports)
                if obj.kind in {"source", "diff"}:
                    raw = snapshot.read(obj.document_id).decode("utf-8").splitlines(keepends=True)
                    span = "".join(raw[obj.start_line - 1:obj.end_line]).encode()
                    if sha256(span) != obj.span_sha256:
                        raise ProtocolError("evidence_span_mismatch")
            elif kind == "handoff":
                receiver = next((r for r in self.roles(c.run_id) if r["id"] == obj.to_role_run_id), None)
                if receiver is None:
                    raise ProtocolError("handoff_recipient_unknown")
                validate_handoff(obj, c, RoleIdentity.parse({k: receiver[k] for k in ("run_id", "role_run_id", "session_id", "role")}), reports, evidence)
            elif kind == "review":
                if role["role"] != "reviewer" or c.candidate_id != meta["candidate_id"]:
                    raise ProtocolError("review_not_for_active_candidate")
                validate_review(obj, c, identity, evidence, docs, reports, snapshot.requirements)
                if any(old.reviewer_role_run_id == role_id and old.review_id != obj.review_id for old in self._objects(db, c.run_id, "review").values()):
                    raise StateConflict("role_already_submitted_a_review")
            elif kind == "response":
                reviews = self._objects(db, c.run_id, "review")
                review = reviews.get(obj.review_id)
                if review is None or obj.review_id != meta["review_id"] or role["phase"] != "revising":
                    raise ProtocolError("response_review_not_active")
                target = self.snapshot(c.run_id, obj.target_candidate_id, db).candidate
                validate_response(obj, review, c, target, identity, evidence, {d.document_id: d for d in target.documents}, reports)
                if any((old.review_id, old.finding_id, old.responder_role_run_id) == (obj.review_id, obj.finding_id, role_id) and old.response_id != obj.response_id for old in self._objects(db, c.run_id, "response").values()):
                    raise StateConflict("finding_already_has_role_response")
            self._put(db, role, kind, getattr(obj, KEYS[kind]), obj.model_dump(mode="json"))
            return obj

    def complete_role(self, role_id, result, expected_revision, *, elapsed_seconds=None):
        if not isinstance(result, dict) or set(result) - {"candidate_id", "review_id", "advice_id", "response_ids", "exit_reason", "candidate_origin"}:
            raise ProtocolError("invalid_role_result")
        with self.transaction() as db:
            role, parent, meta = self._active(db, role_id, expected_revision)
            phase = role["phase"]
            if "candidate_origin" in result and (phase != "repairing" or parent["collaboration_mode"] != "dual_review_diagnostic" or result["candidate_origin"] != "manual_seed_confirmed_by_model"):
                raise ProtocolError("invalid_diagnostic_candidate_origin")
            if db.execute("SELECT 1 FROM logical_requests WHERE role_id=? AND state IN ('prepared','dispatching')", (role_id,)).fetchone():
                raise StateConflict("role_has_unsettled_logical_request")
            candidate_id, review_id = meta["candidate_id"], meta["review_id"]
            next_phase = "finalizing"
            if phase == "consulting":
                advice = self._objects(db, role["run_id"], "consultation").get(result.get("advice_id"))
                if advice is None or advice.grant.role_run_id != role_id:
                    raise ProtocolError("consultation_not_produced_by_role")
                next_phase = "repairing"
            elif phase in {"repairing", "revising"}:
                candidate_id = result.get("candidate_id") or candidate_id
                snapshot = self.snapshot(role["run_id"], candidate_id, db)
                if phase == "repairing" or candidate_id != meta["candidate_id"]:
                    if snapshot.candidate.producer_role_run_id != role_id:
                        raise ProtocolError("candidate_not_produced_by_role")
                if snapshot.candidate.patch_sha256 == sha256(b""):
                    raise ProtocolError("no_nonempty_candidate")
                next_phase = "reviewing" if phase == "repairing" else "final_review"
                responses = self._objects(db, role["run_id"], "response")
                for response_id in result.get("response_ids", []):
                    if response_id not in responses or responses[response_id].responder_role_run_id != role_id:
                        raise ProtocolError("response_not_produced_by_role")
            else:
                review_id = result.get("review_id")
                review = self._objects(db, role["run_id"], "review").get(review_id)
                if review is None or review.reviewer_role_run_id != role_id or review.candidate_id != candidate_id:
                    raise ProtocolError("review_not_produced_for_candidate")
                if phase == "reviewing" and review.verdict == "request_changes":
                    next_phase = "revising"
            role.update(status="succeeded", ended=now(), result=result, output_digest=sha256(canonical_bytes(result)))
            role["elapsed_seconds"] = elapsed_seconds
            db.execute("UPDATE role_runs SET status='succeeded',payload=? WHERE id=?", (canonical_bytes(role).decode(), role_id))
            db.execute("UPDATE collaboration_runs SET revision=revision+1,active_role=NULL,phase=?,candidate_id=?,review_id=? WHERE run_id=?", (next_phase, candidate_id, review_id, role["run_id"]))
            self._parent(db, role["run_id"], stage=next_phase)
            self.emit(db, role["run_id"], "role_completed", {"role_run_id": role_id, "phase": phase, "next_phase": next_phase, "output_digest": role["output_digest"]}, role["role"])

    def continue_without_document(self, role_id, reason_code, expected_revision, *, elapsed_seconds=None):
        """Record a failed optional consultation, never invent advice or revive it."""
        allowed = {"document_repeated_evidence_limit", "document_repeated_format_errors", "document_context_limit", "document_deadline"}
        if reason_code not in allowed:
            raise ProtocolError("unsupported_document_failure")
        with self.transaction() as db:
            role, parent, meta = self._active(db, role_id, expected_revision)
            if role["phase"] != "consulting" or parent["collaboration_policy"].get("document_failure_policy") != "continue_without_document_advice_v1":
                raise ProtocolError("document_fallback_not_declared")
            if self._objects(db, role["run_id"], "consultation") or db.execute("SELECT 1 FROM logical_requests WHERE role_id=? AND state!='received'", (role_id,)).fetchone():
                raise StateConflict("document_output_or_request_unsettled")
            unavailable = {"status": "unavailable", "reason_code": reason_code, "failed_role_run_id": role_id,
                "scope": "Program-recorded consultation failure; no document advice or source evidence. Repair may use only the original issue/code and existing permissions."}
            result = {"exit_reason": reason_code, "advice_id": None}
            role.update(status="failed", ended=now(), reason=reason_code, result=result,
                output_digest=sha256(canonical_bytes(result)), elapsed_seconds=elapsed_seconds)
            db.execute("UPDATE role_runs SET status='failed',payload=? WHERE id=?", (canonical_bytes(role).decode(), role_id))
            db.execute("UPDATE collaboration_runs SET phase='repairing',active_role=NULL,revision=revision+1 WHERE run_id=?", (role["run_id"],))
            self._parent(db, role["run_id"], stage="repairing", document_unavailable=unavailable, collaboration_result="degraded")
            self.emit(db, role["run_id"], "document_unavailable", unavailable, "system")
            return unavailable

    def finish(self, run_id):
        with self.transaction() as db:
            parent, meta = self._load(db, run_id)
            if parent["status"] in TERMINAL:
                return parent
            if parent["cancel_requested"]:
                return self._stop(db, run_id, "cancelled", "Parent cancellation requested")
            if meta["phase"] != "finalizing" or meta["active_role"]:
                raise StateConflict("not_ready_to_finalize")
            self.snapshot(run_id, meta["candidate_id"], db).verify()
            review = self._objects(db, run_id, "review").get(meta["review_id"])
            verdict = review.verdict if review else "unavailable"
            all_reviews = self._objects(db, run_id, "review")
            responses = self._objects(db, run_id, "response")
            dispositions = {}
            for old in all_reviews.values():
                if old.review_id == meta["review_id"]:
                    continue
                for finding in old.findings:
                    response = next((r for r in responses.values() if (r.review_id, r.finding_id) == (old.review_id, finding.finding_id)), None)
                    dispositions[finding.finding_id] = response.disposition if response else "unanswered"
            if verdict == "approve" and any(value in {"unanswered", "needs_evidence"} for value in dispositions.values()):
                verdict = "insufficient_evidence"
            result = self._parent(db, run_id, status="finished", stage="done", ended=now(), repair_result="unknown",
                collaboration_result="degraded" if parent.get("document_unavailable") else "completed", review_verdict=verdict, final_candidate_id=meta["candidate_id"],
                finding_dispositions=dispositions,
                reason="Offline orchestration completed; no production repair acceptance is inferred.",
                **({"pause_requested": False} if parent.get("harness_pause_version") == 1 else {}))
            self._skip_pending(db, run_id, "Not required by this feedback branch")
            db.execute("UPDATE collaboration_runs SET phase='done',revision=revision+1 WHERE run_id=?", (run_id,))
            self.emit(db, run_id, "collaboration_finished", {"review_verdict": verdict, "repair_result": "unknown"})
            return result

    def _skip_pending(self, db, run_id, reason):
        for row in db.execute("SELECT id,payload FROM role_runs WHERE run_id=? AND status='pending'", (run_id,)).fetchall():
            payload = json.loads(row["payload"]) | {"status": "skipped", "reason": reason, "ended": now()}
            db.execute("UPDATE role_runs SET status='skipped',payload=? WHERE id=?", (canonical_bytes(payload).decode(), row["id"]))

    def _stop(self, db, run_id, status, reason):
        parent, meta = self._load(db, run_id)
        if parent["status"] in TERMINAL:
            # Queued cancellation may already have ended the parent in RunStore.
            self._skip_pending(db, run_id, reason)
            return parent
        if parent["cancel_requested"] and status != "interrupted":
            status = "cancelled"
        for row in db.execute("SELECT id,payload FROM role_runs WHERE run_id=? AND status='running'", (run_id,)).fetchall():
            role = json.loads(row["payload"]) | {"status": status if status in {"cancelled", "interrupted"} else "failed", "reason": reason,
                                               "ended": None if status == "interrupted" else now()}
            if status == "interrupted":
                role["interruption_detected"] = now()
            db.execute("UPDATE role_runs SET status=?,payload=? WHERE id=?", (role["status"], canonical_bytes(role).decode(), row["id"]))
        self._skip_pending(db, run_id, reason)
        db.execute("UPDATE logical_requests SET state='outcome_unknown' WHERE run_id=? AND state IN ('prepared','dispatching')", (run_id,))
        db.execute("UPDATE request_attempts SET state='outcome_unknown' WHERE request_id IN (SELECT id FROM logical_requests WHERE run_id=?) AND state IN ('reserved','dispatching')", (run_id,))
        db.execute("UPDATE collaboration_runs SET active_role=NULL,phase='done',revision=revision+1 WHERE run_id=?", (run_id,))
        fields = {"interruption_detected": now()} if status == "interrupted" else {}
        if parent.get("harness_pause_version") == 1:
            fields["pause_requested"] = False
        result = self._parent(db, run_id, status=status, stage="done", ended=None if status == "interrupted" else now(), repair_result="unknown", collaboration_result="not_completed", review_verdict="unavailable", reason=reason, **fields)
        self.emit(db, run_id, "collaboration_stopped", {"status": status, "reason": reason})
        return result

    def stop(self, run_id, status, reason):
        if status not in {"failed", "cancelled", "interrupted"}:
            raise ValueError("Invalid stop status")
        with self.transaction() as db:
            return self._stop(db, run_id, status, reason)

    def attach_live_result(self, run_id, kind, fields):
        """Append trusted post-run assessment/receipt metadata without revival.

        The ordinary RunStore.update still rejects all terminal writes. These
        two explicit immutable attachments never change parent or role status.
        """
        allowed = {"assessment": {"repair_result", "final_candidate_id", "assessment_report_id", "collaboration_result", "review_verdict", "reason"},
                   "receipts": {"metrics", "artifacts"},
                   "assessment_error": {"repair_result", "collaboration_result", "reason"}}
        if kind not in allowed or not isinstance(fields, dict) or set(fields) - allowed[kind]:
            raise ProtocolError("invalid_live_result_attachment")
        with self.transaction() as db:
            parent, meta = self._load(db, run_id)
            if parent["kind"] != "collaboration_live" or parent["status"] not in TERMINAL:
                raise StateConflict("live_attachment_requires_terminal_run")
            if kind == "assessment":
                c = self.snapshot(run_id, fields["final_candidate_id"], db).candidate
                report = self._objects(db, run_id, "report").get(fields["assessment_report_id"])
                if report is None or report.origin != "program_test" or (report.candidate_id, report.source_tree_digest) != (c.candidate_id, c.source_tree_digest):
                    raise ProtocolError("live_assessment_not_bound_to_program_report")
                if fields["repair_result"] not in {"passed", "failed", "unknown"} or fields["repair_result"] == "passed" and report.status != "passed":
                    raise ProtocolError("live_assessment_not_supported")
            if kind == "assessment_error" and (fields.get("repair_result"), fields.get("collaboration_result")) != ("unknown", "degraded"):
                raise ProtocolError("assessment_error_cannot_claim_success")
            key, content = "live_" + kind + "_digest", sha256(canonical_bytes(fields))
            if key in parent:
                if parent[key] != content:
                    raise StateConflict("immutable_live_result_attachment")
                return parent
            parent = self._parent(db, run_id, **fields, **{key: content})
            self.emit(db, run_id, "live_" + kind + "_attached", {"sha256": content})
            return parent

    def cancel(self, run_id):
        if self.get(run_id)["kind"] == "collaboration_history":
            raise ValueError("Archived collaboration is read-only; never resume or cancel it.")
        result = super().cancel(run_id)
        if result["status"] == "cancelled" and result["kind"] in {"collaboration_mock", "collaboration_live"}:
            with self.transaction() as db:
                self._skip_pending(db, run_id, "Cancelled before role start")
                db.execute("UPDATE collaboration_runs SET phase='done',revision=revision+1 WHERE run_id=? AND phase!='done'", (run_id,))
                result = self._parent(db, run_id, stage="done")
        return result

    def reconcile(self):
        # Supervisor owns worker.lock; keep parent and role interruption atomic.
        self.reconcile_collaborations()
        super().reconcile()

    def reconcile_collaborations(self):
        """Caller must own collaboration.lock; no replay or automatic resume."""
        for parent in self.list():
            if parent["kind"] in {"collaboration_mock", "collaboration_live"} and parent["status"] in {"running", "cancel_requested"}:
                self.stop(parent["id"], "interrupted", "Worker exited; preserve receipts and do not resend or resume.")

    def prepare_request(self, role_id, request_key, input_digest):
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 120 or not isinstance(input_digest, str) or len(input_digest) != 64 or any(c not in "0123456789abcdef" for c in input_digest):
            raise ValueError("Invalid logical request identity")
        with self.transaction() as db:
            role, parent, meta = self._active(db, role_id)
            old = db.execute("SELECT * FROM logical_requests WHERE role_id=? AND request_key=?", (role_id, request_key)).fetchone()
            if old:
                if old["input_digest"] != input_digest:
                    raise StateConflict("logical_request_key_content_conflict")
                return dict(old)
            request_id = uuid.uuid4().hex
            payload = {"created": now(), "session_id": role["session_id"]}
            db.execute("INSERT INTO logical_requests VALUES(?,?,?,?,?,'prepared',?)", (request_id, role["run_id"], role_id, request_key, input_digest, canonical_bytes(payload).decode()))
            self.emit(db, role["run_id"], "logical_request_prepared", {"logical_request_id": request_id, "role_run_id": role_id}, role["role"])
        return self.request(request_id)

    def request(self, request_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM logical_requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise KeyError("Logical request not found")
        return dict(row)

    def check_request(self, request_id, role_id):
        with self.transaction() as db:
            role, parent, meta = self._active(db, role_id)
            row = db.execute("SELECT * FROM logical_requests WHERE id=? AND role_id=?", (request_id, role_id)).fetchone()
            if row is None or row["state"] not in {"prepared", "dispatching"}:
                raise StateConflict("request_not_available_for_dispatch")
            return dict(row)

    def request_event(self, request_id, role_id, kind, data, state=None):
        with self.transaction() as db:
            role, parent, meta = self._active(db, role_id)
            row = db.execute("SELECT state FROM logical_requests WHERE id=? AND role_id=?", (request_id, role_id)).fetchone()
            if row is None or row[0] not in {"prepared", "dispatching"}:
                raise StateConflict("request_already_terminal")
            if state is not None:
                if state not in {"dispatching", "received", "outcome_unknown"}:
                    raise ValueError("Invalid request transition")
                db.execute("UPDATE logical_requests SET state=? WHERE id=?", (state, request_id))
            if kind == "usage_reserved":
                db.execute("INSERT INTO request_attempts VALUES(?,?,?,'reserved')", (data["ticket"], request_id, data["provider"]))
            elif kind in {"request_dispatching", "request_receipt_recorded", "request_attempt_failed"}:
                attempt = db.execute("SELECT state FROM request_attempts WHERE ticket=? AND request_id=?", (data["ticket"], request_id)).fetchone()
                expected = "reserved" if kind == "request_dispatching" else "dispatching"
                if attempt is None or attempt[0] != expected:
                    raise StateConflict("attempt_already_dispatched_or_not_active")
                target = {"request_dispatching": "dispatching", "request_receipt_recorded": "received", "request_attempt_failed": "failed"}[kind]
                db.execute("UPDATE request_attempts SET state=? WHERE ticket=?", (target, data["ticket"]))
            self.emit(db, role["run_id"], kind, {**data, "logical_request_id": request_id, "role_run_id": role_id}, role["role"])
