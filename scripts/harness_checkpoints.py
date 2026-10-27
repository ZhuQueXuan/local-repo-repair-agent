"""Opt-in clean role-boundary checkpoints and linked continuation identities.

No transport, credentials, pickles, eval or automatic recovery. Paused sources
are immutable; only verified checkpoint bytes seed one explicitly created child.
"""
from copy import deepcopy
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid

from collaboration_protocol import canonical_bytes, sha256, ProtocolError
from task_service_store import ROOT, now, worker_lock
from task_cards import card_view, validate_card
from review_tools import _read_bytes

class CheckpointConflict(ValueError):
    pass


def supported(row):
    return row.get("harness_pause_version") == 1 and row["kind"] == "collaboration_live" and row["collaboration_mode"] in {"dual_review_live", "document_review_live"}


def request_pause(store, run_id):
    with store.transaction() as db:
        parent, meta = store._load(db, run_id)
        if not supported(parent) or parent["status"] not in {"queued", "running"} or parent["cancel_requested"]:
            raise CheckpointConflict("此任务不能暂停。仅新建时开启阶段暂停的双/三角色任务可用。")
        if meta["phase"] == "finalizing":
            raise CheckpointConflict("所有角色已结束，当前正在收尾，不能再生成暂停点。")
        if not parent.get("pause_requested"):
            parent = store._parent(db, run_id, pause_requested=True)
            store.emit(db, run_id, "pause_requested", {"scope": "Wait for current whole role; do not interrupt tools or requests."}, "user")
        return parent


def checkpoint_view(store, run_id):
    row = store.get(run_id)
    value = {"enabled": supported(row), "can_pause": supported(row) and row["status"] in {"queued", "running"} and not row["cancel_requested"] and row["stage"] != "finalizing",
        "pause_requested": bool(row.get("pause_requested")) and row["status"] in {"queued", "running"}, "can_continue": False,
        "continued_from": row.get("continued_from"), "root_run_id": row.get("root_run_id", run_id),
        "note": "阶段暂停等待当前完整角色结束；最终收尾优先。续做创建关联新任务，只执行未完成阶段，不补发旧请求。"}
    if row["status"] == "paused" and supported(row):
        value.update(checkpoint_sha256=row.get("checkpoint_sha256"), next_phase=row.get("checkpoint_next_phase"),
            remaining_seconds=row.get("checkpoint_remaining_seconds"), ready=bool(row.get("live_receipts_digest")))
        with store.connect() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='harness_continuations'").fetchone()
            child = db.execute("SELECT * FROM harness_continuations WHERE source_id=?", (run_id,)).fetchone() if exists else None
        if child:
            value.update(child_run_id=child["child_id"], continuation_state=child["state"])
        else:
            value["can_continue"] = value["ready"]
    return value


def save_checkpoint(runner):
    store, run_id = runner.store, runner.run_id
    state = store.state(run_id)
    meta = state["collaboration_state"]
    if not supported(state) or meta["active_role"] or not state.get("pause_requested") or meta["phase"] not in {"consulting", "repairing", "reviewing", "revising", "final_review"}:
        raise CheckpointConflict("只能在无活动角色的安全边界保存暂停点。")
    if any(digest_file(ROOT / name) != expected for name, expected in runner.frozen.items()):
        raise CheckpointConflict("运行代码或冻结输入已改变，不能保存可续做暂停点。")
    remaining = math.floor(runner.parent_deadline - time.monotonic())
    if remaining < 1:
        raise TimeoutError("No remaining parent time for a checkpoint")
    from task_workspace import load_task
    source, manifest = load_task(state["task"])
    from review_tools import seal_candidate
    checked = seal_candidate(manifest, source / "repo", runner.work, runner.issue,
        store.home / "sealing" / ("checkpoint-check-" + uuid.uuid4().hex), run_id=run_id,
        producer_role_run_id=next(r["id"] for r in store.roles(run_id) if r["phase"]=="repairing"))
    if meta["candidate_id"]:
        current = store.snapshot(run_id, meta["candidate_id"]).candidate
        if (checked.candidate.source_tree_digest, checked.candidate.patch_sha256) != (current.source_tree_digest,current.patch_sha256):
            raise CheckpointConflict("工作副本与已提交候选不一致，不能继续。")
    elif checked.candidate.patch_sha256 != sha256(b""):
        raise CheckpointConflict("未修复阶段工作副本发生了改变。")
    session = runner.restored_session
    if runner.session is not None:
        runner.session.submitted_history()  # Prove the completed submit call.
        agent = runner.session.agent
        session = {"messages": agent.messages, "submission": agent.env.completed_submission,
            "extra_template_vars": agent.extra_template_vars,
            "tool_output_events": agent.env.output_budget.events,
            "task_state": {key: getattr(agent.env.task_state,key) for key in
                ("original_files","repeats","blocked_since_change","events","evidence","last_intent","warning")},
            "execution_progress": agent.env.task_state.progress.export_state() if agent.env.task_state.progress else None,
            "scope": "Imported author history, not replayed actions or a resumed provider request."}
    if meta["phase"] in {"reviewing", "revising", "final_review"} and session is None:
        raise CheckpointConflict("缺少已确认提交的修复历史，不能建立续做点。")
    directory = runner.output / "checkpoint"
    directory.mkdir(exist_ok=False)
    files = {}
    total = 0
    def put(name, raw):
        nonlocal total
        total += len(raw)
        if total > 64 * 1024 * 1024:
            raise CheckpointConflict("暂停点超过64MB，停止而不截断证据。")
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(raw)
        files[name] = sha256(raw)
    # Fixed manifest files only; temp helpers and caches are never restored.
    for name in manifest["source_files_sha256"]:
        put("work/" + name, _read_bytes(runner.work, name))
    if session is not None:
        from collaboration_panel import checked_bytes
        for event in session.get("tool_output_events", []):
            name = event["full_output_path"]
            if not name.startswith("temp/tool-output/") or not Path(name).stem.isdigit() or Path(name).suffix != ".txt":
                raise CheckpointConflict("工具输出归档路径不合法。")
            raw = checked_bytes(runner.work,name,limit=16*1024*1024)
            if sha256(raw) != event["sha256"]:
                raise CheckpointConflict("原工具输出归档发生变化。")
            put("work/"+name,raw)
        history=runner.work/"temp/context-history"
        if history.is_dir():
            for path in history.glob("*.txt"):
                if not path.stem.isdigit():raise CheckpointConflict("历史归档名称不合法。")
                index=int(path.stem)
                raw=checked_bytes(runner.work,path.relative_to(runner.work).as_posix(),limit=16*1024*1024)
                if index>=len(session["messages"]) or raw!=session["messages"][index].get("content","").encode():
                    raise CheckpointConflict("历史归档与原消息不一致。")
                put("work/"+path.relative_to(runner.work).as_posix(),raw)
        notes=runner.work/"temp/task-state/notes.json"
        if notes.exists():
            put("work/temp/task-state/notes.json",checked_bytes(runner.work,"temp/task-state/notes.json",limit=16384))
    for name, expected in manifest["task_files_sha256"].items():
        if name == "issue.md":
            if _read_bytes(runner.work, "TASK.md") != runner.issue:
                raise CheckpointConflict("任务说明发生变化。")
        elif name.startswith("checks/"):
            if sha256(_read_bytes(runner.work, "task_checks/" + name[len("checks/"):])) != expected:
                raise CheckpointConflict("检查文件发生变化。")
    objects = []
    with store.connect() as db:
        if db.execute("SELECT 1 FROM logical_requests WHERE run_id=? AND state!='received'", (run_id,)).fetchone():
            raise CheckpointConflict("存在未确认结果的模型请求；不自动重发或生成续做点。")
        rows = db.execute("SELECT * FROM collaboration_objects WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
        for item in rows:
            if sha256(item["payload"].encode()) != item["digest"]:
                raise CheckpointConflict("注册证据发生变化。")
            payload = json.loads(item["payload"])
            if item["kind"] == "snapshot":
                snap = store.snapshot(run_id, item["id"], db)
                for doc in snap.candidate.documents:
                    put("objects/" + item["id"] + "/" + doc.document_id, snap.read(doc.document_id))
            elif item["kind"] == "consultation":
                from document_agent import ConsultationRecord, validate_consultation
                snap = validate_consultation(ConsultationRecord.parse(payload), store.home)
                for doc in snap.grant.documents:
                    put("consultation/" + doc.document_id, snap.read(doc.document_id))
            objects.append(dict(item))
    assessments = deepcopy(runner.assessments)
    for candidate_id, assessment in assessments.items():
        name = "patches/" + candidate_id + ".diff"
        from collaboration_panel import checked_bytes
        path = Path(assessment["patch"])
        put(name, checked_bytes(runner.output, path.relative_to(runner.output).as_posix()))
        assessment["patch"] = name
    payload = {"schema_version": 1, "source_run_id": run_id, "next_phase": meta["phase"], "meta": meta,
        "remaining_parent_seconds": remaining, "frozen": runner.frozen, "roles": store.roles(run_id),
        "objects": objects, "files_sha256": files, "assessments": assessments, "baseline": runner.baseline,
        "route_state": runner.route_state, "session": session, "parent": {k: state[k] for k in ("task", "profile", "configuration", "collaboration_policy", "task_digest", "policy_digest", "document_unavailable") if k in state}}
    from execution_runtime import runtime_identity
    payload["execution_identity"] = runtime_identity()
    raw = canonical_bytes(payload)
    if len(raw) > 16 * 1024 * 1024:
        raise CheckpointConflict("暂停点清单超过16MB，不截断后续恢复所需历史。")
    if runner.models and any(model.contains_credentials(raw.decode()) for model in runner.models):
        raise ProtocolError("credential_in_checkpoint")
    path = directory / "manifest.json"
    path.write_bytes(raw)
    relative = path.relative_to(store.home).as_posix()
    with store.transaction() as db:
        parent, current = store._load(db, run_id)
        if parent["cancel_requested"] or parent["status"] != "running" or current != meta:
            raise CheckpointConflict("保存暂停点时任务状态改变；不使用该未登记副本。")
        result = store._parent(db, run_id, status="paused", stage="paused", ended=now(), pause_requested=False,
            checkpoint_path=relative, checkpoint_sha256=sha256(raw), checkpoint_next_phase=meta["phase"], checkpoint_remaining_seconds=remaining,
            collaboration_result="paused", review_verdict="not_run", repair_result="unknown",
            reason="已在完整角色结束后的边界暂停；原记录保留。继续会核对检查点并创建关联新任务。")
        store.emit(db, run_id, "checkpoint_saved", {"sha256": sha256(raw), "next_phase": meta["phase"], "remaining_seconds": remaining})
    return result


def digest_file(path):
    return sha256(path.read_bytes())


def load_checkpoint(store, parent):
    from collaboration_panel import checked_bytes
    expected = "runs/" + parent["id"] + "/checkpoint/manifest.json"
    if parent.get("checkpoint_path") != expected:
        raise CheckpointConflict("暂停点目录不匹配。")
    directory = store.home / "runs" / parent["id"] / "checkpoint"
    raw = checked_bytes(store.home, expected, limit=16 * 1024 * 1024)
    if sha256(raw) != parent["checkpoint_sha256"]:
        raise CheckpointConflict("暂停点清单哈希不匹配。")
    value = json.loads(raw)
    if value["source_run_id"] != parent["id"] or value["next_phase"] != parent["checkpoint_next_phase"]:
        raise CheckpointConflict("暂停点身份不匹配。")
    from execution_runtime import runtime_identity
    if value.get("execution_identity") != runtime_identity():
        raise CheckpointConflict("执行环境声明发生变化；不跨Python、Bash或依赖环境续做。请保留原记录后另建任务。")
    for name, expected in value["frozen"].items():
        if not (ROOT / name).is_file() or digest_file(ROOT / name) != expected:
            raise CheckpointConflict("运行代码/输入已改变，暂停点不能直接续做：" + name)
    for name, expected in value["files_sha256"].items():
        if sha256(checked_bytes(directory, name)) != expected:
            raise CheckpointConflict("暂停点文件发生变化。")
    if value["remaining_parent_seconds"] < 1:
        raise CheckpointConflict("任务没有剩余运行时间。")
    return value, directory


def ensure_continuations(store):
    with store.connect() as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='harness_continuations'").fetchone():
            return
        with sqlite3.connect(store.home / ("before_harness_continuations_" + uuid.uuid4().hex + ".sqlite3")) as target:
            db.backup(target)
        db.execute("CREATE TABLE IF NOT EXISTS harness_continuations(source_id TEXT PRIMARY KEY,request_key TEXT NOT NULL UNIQUE,child_id TEXT,state TEXT NOT NULL,checkpoint_sha256 TEXT NOT NULL)")


def continue_checkpoint(store, run_id, body):
    if not isinstance(body, dict) or set(body) != {"request_key", "checkpoint_sha256", "task_card"}:
        raise ValueError("续做请求字段不完整，请刷新。")
    key = body["request_key"]
    if not isinstance(key, str) or not 1 <= len(key) <= 120:
        raise ValueError("需要固定的续做请求键。")
    card = validate_card(body["task_card"])
    # The current worker must have saved receipts and exited, including after
    # service restart. Acquiring this lock never terminates a running worker.
    with worker_lock(store.home) as acquired:
        if not acquired:
            raise CheckpointConflict("执行器仍在收尾，请稍后刷新再继续。")
        parent = store.get(run_id)
        if not supported(parent) or parent["status"] != "paused" or not parent.get("live_receipts_digest") or body["checkpoint_sha256"] != parent["checkpoint_sha256"]:
            raise CheckpointConflict("只支持当前已完整封存的暂停任务；旧失败/中断/取消身份不恢复。")
        if parent.get("continuation_depth", 0) >= 63:
            raise CheckpointConflict("续做链已达到64段保护，请先人工复核。")
        value, directory = load_checkpoint(store, parent)
        ensure_continuations(store)
        with store.transaction() as db:
            old = db.execute("SELECT * FROM harness_continuations WHERE source_id=?", (run_id,)).fetchone()
            if old:
                if old["request_key"] != key:
                    raise CheckpointConflict("此暂停点已有续做请求，不创建第二个任务。")
                if old["child_id"]:
                    child = store.get(old["child_id"])
                    if child.get("task_card_seed_sha256") != sha256(canonical_bytes(card)):
                        raise CheckpointConflict("同一续做请求的任务卡发生变化。")
                    return child
                raise CheckpointConflict("上次续做准备未完成，保留该身份并等待核对，不自动另建。")
            if db.execute("SELECT 1 FROM runs WHERE request_key=?", (key,)).fetchone():
                raise CheckpointConflict("续做请求键已属于其他任务。")
            db.execute("INSERT INTO harness_continuations VALUES(?,?,NULL,'preparing',?)", (run_id, key, parent["checkpoint_sha256"]))
        child = store.create_collaboration(parent["task"], parent["profile"], key, parent["collaboration_policy"], execution="live", task_card=card, allow_pause=True, allow_capabilities=parent.get("harness_capabilities_version") == 1, model_profile=parent.get('model_profile_id'), _defer_dispatch=True)
        with store.transaction() as db:
            db.execute("UPDATE harness_continuations SET child_id=? WHERE source_id=?", (child["id"], run_id))
        try:
            id_map = import_objects(store, child, value, directory)
            with store.transaction() as db:
                meta = value["meta"]
                store._parent(db, child["id"], continued_from=run_id, root_run_id=parent.get("root_run_id", run_id),
                    continuation_depth=parent.get("continuation_depth", 0) + 1,
                    continuation_checkpoint_sha256=parent["checkpoint_sha256"], continuation_id_map=id_map,
                    remaining_parent_seconds=value["remaining_parent_seconds"], stage=value["next_phase"],
                    transport_scope=parent.get("transport_scope", "live"),
                    scope=parent["scope"] + " Linked continuation; completed outputs/history imported, not new model achievements.")
                db.execute("UPDATE collaboration_runs SET phase=?,candidate_id=?,review_id=? WHERE run_id=?", (value["next_phase"], id_map.get(meta["candidate_id"], meta["candidate_id"]), meta["review_id"], child["id"]))
                store.emit(db, child["id"], "checkpoint_imported", {"continued_from": run_id, "checkpoint_sha256": parent["checkpoint_sha256"], "next_phase": value["next_phase"], "scope": "New identity; no old command/model replay"})
                db.execute("UPDATE harness_continuations SET state='ready' WHERE source_id=?", (run_id,))
                store._parent(db, child["id"], status="queued")
        except Exception:
            store.stop(child["id"], "failed", "检查点导入失败；保留新身份，不自动重试或另建。")
            with store.transaction() as db:
                db.execute("UPDATE harness_continuations SET state='failed' WHERE source_id=?", (run_id,))
            raise
        return store.get(child["id"])


def import_objects(store, child, value, directory):
    """Rebind immutable provenance to new role IDs; retain an explicit map."""
    from collaboration_store import TYPES, KEYS
    from collaboration_protocol import Candidate, Requirement, EvidenceRef, validate_evidence, validate_review, validate_response, validate_handoff, RoleIdentity
    from review_tools import ReviewSnapshot
    from document_agent import DocumentGrant, ConsultationRecord, validate_consultation
    run_id = child["id"]
    old_roles = {r["phase"]: r for r in value["roles"]}
    new_roles = {r["phase"]: r for r in store.roles(run_id)}
    mapping = {value["source_run_id"]: run_id}
    for phase, role in old_roles.items():
        mapping[role["id"]] = new_roles[phase]["id"]
        mapping[role["session_id"]] = new_roles[phase]["session_id"]
    def rebind(obj):
        if isinstance(obj, str): return mapping.get(obj, obj)
        if isinstance(obj, list): return [rebind(x) for x in obj]
        if isinstance(obj, dict): return {k: rebind(v) for k,v in obj.items()}
        return obj
    snapshots, objects, stored = {}, {}, []
    for row in value["objects"]:
        if row["kind"] != "snapshot": continue
        old = json.loads(row["payload"])
        c = old["candidate"]
        ident = sha256(canonical_bytes([run_id,c["task_digest"],c["baseline_digest"],c["source_tree_digest"],c["patch_sha256"]]))[:32]
        mapping[c["candidate_id"]] = "candidate-" + ident
        mapping[c["snapshot_id"]] = "snapshot-" + ident
        candidate = Candidate.parse(rebind(c))
        dest = store.home / "snapshots" / sha256(candidate.candidate_id.encode())
        (dest / "objects").mkdir(parents=True, exist_ok=False)
        for doc in candidate.documents:
            (dest / "objects" / doc.document_id).write_bytes(_read_bytes(directory, "objects/"+c["candidate_id"]+"/"+doc.document_id))
        raw = canonical_bytes(candidate)
        (dest / "candidate.json").write_bytes(raw)
        snap = ReviewSnapshot(dest,candidate,sha256(raw),tuple(Requirement.parse(r) for r in old["requirements"]))
        snap.verify();snapshots[candidate.candidate_id]=snap
        payload={"candidate":candidate.model_dump(mode="json"),"relative_directory":dest.relative_to(store.home).as_posix(),"manifest_sha256":sha256(raw),"requirements":old["requirements"]}
        stored.append(("snapshot",candidate.candidate_id,mapping[row["role_id"]],payload))
    for row in value["objects"]:
        if row["kind"] != "consultation":continue
        payload=json.loads(row["payload"]);grant=rebind(payload["grant"])
        fields={k:v for k,v in grant.items() if k not in {"snapshot_id","schema_version"}}
        mapping[payload["grant"]["snapshot_id"]]="documents-"+sha256(canonical_bytes(fields))[:32]
        grant=DocumentGrant.parse(rebind(payload["grant"]))
        dest=store.home/"document_snapshots"/run_id/grant.role_run_id
        (dest/"objects").mkdir(parents=True,exist_ok=False)
        for doc in grant.documents:(dest/"objects"/doc.document_id).write_bytes(_read_bytes(directory,"consultation/"+doc.document_id))
        (dest/"grant.json").write_bytes(canonical_bytes(grant))
        for ref in payload["evidence"]:
            mapping[ref["evidence_id"]]="doc-evidence-"+sha256(canonical_bytes([grant.snapshot_id,ref["document_id"],ref["start_line"],ref["end_line"],ref["span_sha256"]]))[:32]
        rebound=rebind(payload);rebound["relative_directory"]=dest.relative_to(store.home).as_posix()
        record=ConsultationRecord.parse(rebound);validate_consultation(record,store.home)
        stored.append(("consultation",record.advice_id,mapping[row["role_id"]],record.model_dump(mode="json")))
    reports={}
    for row in value["objects"]:
        if row["kind"]=="report":
            obj=TYPES["report"].parse(rebind(json.loads(row["payload"])))
            reports[obj.report_id]=obj
            stored.append(("report",obj.report_id,mapping[row["role_id"]],obj.model_dump(mode="json")))
    for row in value["objects"]:
        if row["kind"]!="evidence":continue
        payload=rebind(json.loads(row["payload"]))
        if payload.get("report_id"):payload["report_sha256"]=sha256(canonical_bytes(reports[payload["report_id"]]))
        old_id=payload.pop("evidence_id")
        # Includes optional fields to avoid relying on legacy ID minting shape.
        mapping[old_id]="ev-"+sha256(canonical_bytes(payload))[:32]
        payload["evidence_id"]=mapping[old_id]
        obj=EvidenceRef.parse(payload);c=snapshots[obj.candidate_id].candidate
        validate_evidence(obj,c,{d.document_id:d for d in c.documents},reports)
        objects.setdefault("evidence",{})[obj.evidence_id]=obj
        stored.append(("evidence",obj.evidence_id,mapping[row["role_id"]],obj.model_dump(mode="json")))
    for kind in ("handoff","review","response"):
        for row in value["objects"]:
            if row["kind"]!=kind:continue
            obj=TYPES[kind].parse(rebind(json.loads(row["payload"])))
            identity=RoleIdentity.parse({k:new_roles[next(p for p,r in old_roles.items() if r["id"]==row["role_id"])][k] for k in ("run_id","role_run_id","session_id","role")})
            candidate_id=obj.reviewed_candidate_id if kind=="response" else obj.candidate_id
            snap=snapshots[candidate_id];c=snap.candidate;docs={d.document_id:d for d in c.documents};evidence=objects.get("evidence",{})
            if kind=="handoff":
                receiver=next(r for r in new_roles.values() if r["id"]==obj.to_role_run_id)
                validate_handoff(obj,c,RoleIdentity.parse({k:receiver[k] for k in ("run_id","role_run_id","session_id","role")}),reports,evidence)
            elif kind=="review":validate_review(obj,c,identity,evidence,docs,reports,snap.requirements)
            else:
                target=snapshots[obj.target_candidate_id].candidate
                validate_response(obj,objects["review"][obj.review_id],c,target,identity,evidence,{d.document_id:d for d in target.documents},reports)
            objects.setdefault(kind,{})[getattr(obj,KEYS[kind])]=obj
            stored.append((kind,getattr(obj,KEYS[kind]),mapping[row["role_id"]],obj.model_dump(mode="json")))
    with store.transaction() as db:
        for kind,key,role_id,payload in stored:
            raw=canonical_bytes(payload)
            db.execute("INSERT INTO collaboration_objects VALUES(?,?,?,?,?,?)",(run_id,kind,key,role_id,sha256(raw),raw.decode()))
        for phase,old in old_roles.items():
            unavailable = value["parent"].get("document_unavailable")
            inherited_failure = (phase == "consulting" and old["status"] == "failed" and unavailable
                and unavailable["failed_role_run_id"] == old["id"]
                and unavailable["reason_code"] == old["reason"])
            if old["status"]=="succeeded" or inherited_failure:
                new=new_roles[phase]
                status = "failed" if inherited_failure else "succeeded"
                new.update(status=status,inherited_from=value["source_run_id"],inherited_role_run_id=old["id"],
                    result=rebind(old["result"]),output_digest=sha256(canonical_bytes(rebind(old["result"]))),
                    reason=old["reason"] if inherited_failure else "Inherited completed output; no model/tool re-execution",elapsed_seconds=0)
                db.execute("UPDATE role_runs SET status=?,payload=? WHERE id=?",(status,canonical_bytes(new).decode(),new["id"]))
                if inherited_failure:
                    store._parent(db, run_id, document_unavailable=rebind(unavailable), collaboration_result="degraded")
    return mapping


def restore_runner(runner):
    """Hydrate a new worker from immutable checkpoint files, not old workspace."""
    child=runner.store.get(runner.run_id)
    source=runner.store.get(child["continued_from"])
    value,directory=load_checkpoint(runner.store,source)
    if source["checkpoint_sha256"]!=child["continuation_checkpoint_sha256"]:
        raise CheckpointConflict("续做点绑定已改变。")
    mapping=child["continuation_id_map"]
    for name in runner.manifest["source_files_sha256"]:
        (runner.work/name).write_bytes(_read_bytes(directory,"work/"+name))
    from collaboration_panel import checked_bytes
    for name in value["files_sha256"]:
        if name.startswith(("work/temp/tool-output/","work/temp/context-history/")) or name=="work/temp/task-state/notes.json":
            path=runner.work/name[len("work/"):]
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(checked_bytes(directory,name,limit=16*1024*1024))
    runner.baseline=value["baseline"]
    runner.route_state.update(value["route_state"])
    for old_id,assessment in value["assessments"].items():
        path=runner.output/(old_id+".diff")
        path.write_bytes(_read_bytes(directory,assessment["patch"]))
        runner.assessments[mapping[old_id]]=dict(assessment,patch=str(path))
    runner.restored_session=value["session"]
    runner.parent_deadline=min(runner.parent_deadline,time.monotonic()+value["remaining_parent_seconds"])
    (runner.output/"continuation-import.json").write_bytes(canonical_bytes({"source":source["id"],"checkpoint_sha256":source["checkpoint_sha256"],"next_phase":value["next_phase"],"id_map":mapping,
        "scope":"Completed evidence rebound with explicit lineage. Imported author history; no prior action or request replay."}))
