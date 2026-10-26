"""Read-only panel projections of registered, version-bound collaboration data."""
from datetime import datetime, timezone
import json
from pathlib import Path

from collaboration_protocol import canonical_bytes, sha256, ProtocolError
from review_tools import _read, _safe_file
from task_service_store import ROOT

KINDS = {"collaboration_live", "collaboration_mock", "collaboration_history"}


def checked_bytes(root, name, limit=32 * 1024 * 1024):
    path = _safe_file(root, name)
    before = path.stat()
    if before.st_nlink != 1 or before.st_size > limit:
        raise ProtocolError("linked_or_oversized_panel_artifact")
    with path.open("rb") as stream:
        import os
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ProtocolError("panel_artifact_changed_during_open")
        raw = stream.read(limit + 1)
    after = _safe_file(root, name).stat()
    if len(raw) > limit or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ProtocolError("panel_artifact_changed_during_read")
    return raw


def public_policy():
    return json.loads((ROOT / "configs/multi_agent_v1.json").read_text(encoding="utf-8"))


def create_panel_run(store, body):
    if set(body) - {"task", "profile", "mode", "request_key", "collaboration_mode", "policy_version"}:
        raise ValueError("Unknown creation fields")
    mode = body.get("collaboration_mode", "single")
    if mode == "single":
        return store.create(body["task"], body["profile"], body["mode"], body["request_key"])
    if mode != "dual_review" or body["mode"] != "live" or body["profile"] != "adaptive_history":
        raise ValueError("双角色目前仅支持真实运行＋上下文与任务状态策略；离线重放没有新的模型审查。")
    if type(body.get("policy_version")) is not int or body["policy_version"] != 1:
        raise ValueError("Refresh the panel policy before creating a collaboration")
    return store.create_collaboration(body["task"], body["profile"], body["request_key"], public_policy(), execution="live")


def usage_view(store, row):
    # Read a stable atomic ledger snapshot. No SharedBudget lock/write and no
    # credentials/client initialization are needed to view existing receipts.
    path = store.home / "synthetic_ledger.json" if row["kind"] == "collaboration_mock" else ROOT / "data/deepseek_budget.json"
    if not path.exists():
        return {"available": False, "reason": "用量尚不可读，保留未知。"}
    ledger = json.loads(path.read_text(encoding="utf-8"))
    from collaboration import attributed_usage
    from contextlib import contextmanager
    class ReadOnlyLedger:
        @contextmanager
        def _locked(self):
            yield ledger
    usage = attributed_usage(ReadOnlyLedger(), row["id"])
    providers = {}
    for ticket, receipt in ledger["requests"].items():
        if receipt.get("attribution", {}).get("run_id") != row["id"]:
            continue
        provider = receipt.get("provider", "unknown")
        item = providers.setdefault(provider, {"attempts": 0, "settled": 0, "unknown": 0})
        item["attempts"] += 1
        item["settled" if receipt["status"] == "settled" else "unknown"] += 1
    return usage | {"available": True, "providers": providers, "synthetic": row["kind"] == "collaboration_mock"}


def collaboration_view(store, run_id):
    row = store.get(run_id)
    if row["kind"] not in KINDS:
        return {"mode": "single", "note": "旧记录/单Agent没有审查角色，不补造。"}
    state = store.state(run_id)["collaboration_state"]
    with store.connect() as db:
        snapshots = db.execute("SELECT id,payload,digest FROM collaboration_objects WHERE run_id=? AND kind='snapshot' ORDER BY rowid LIMIT 251", (run_id,)).fetchall()
    candidates = []
    for entry in snapshots[:250]:
        if sha256(entry["payload"].encode()) != entry["digest"]:
            raise ProtocolError("stored_snapshot_hash_mismatch")
        c = json.loads(entry["payload"])["candidate"]
        candidates.append({k: c[k] for k in ("candidate_id", "source_tree_digest", "producer_role_run_id", "patch_sha256")})
    records = {}
    for kind in ("report", "handoff", "review", "response", "evidence"):
        values = list(store.records(run_id, kind).values())
        records[kind + "s"] = [v.model_dump(mode="json") for v in values[:250]]
        if len(values) > 250:
            records.setdefault("truncated_kinds", []).append(kind)
    try:
        usage = usage_view(store, row)
    except (ValueError, OSError):
        usage = {"available": False, "reason": "本次用量读取失败，不能按零费用显示。"}
    end = row.get("ended")
    elapsed = None
    if row.get("created"):
        until = datetime.fromisoformat(end) if end else datetime.now(timezone.utc) if row["status"] in {"running", "cancel_requested"} else None
        if until:
            elapsed = round((until - datetime.fromisoformat(row["created"])).total_seconds(), 3)
    measured_elapsed = row.get("metrics", {}).get("total_elapsed_seconds")
    result = {"mode": row["collaboration_mode"], "state": state, "roles": store.roles(run_id), "candidates": candidates,
        "candidates_truncated": len(snapshots) > 250, "usage": usage, "total_elapsed_seconds": measured_elapsed if measured_elapsed is not None else elapsed,
        "elapsed_scope": "准备至收尾总秒数" if measured_elapsed is not None else "创建至流程结束秒数（收尾耗时未知）",
        "collaboration_result": row["collaboration_result"], "review_verdict": row["review_verdict"],
        "final_candidate_id": row.get("final_candidate_id"), "synthetic": row["kind"] == "collaboration_mock", **records}
    if len(canonical_bytes(result)) > 2 * 1024 * 1024:
        raise ValueError("Collaboration index exceeds bounded preview")
    return result


def candidate_view(store, run_id, candidate_id):
    snapshot = store.snapshot(run_id, candidate_id)  # checks run, all hashes and links
    c = snapshot.candidate
    docs = [d for d in c.documents if d.side == "diff"]
    diff = snapshot.read(docs[0].document_id).decode("utf-8") if docs else ""
    return {"candidate_id": c.candidate_id, "source_tree_digest": c.source_tree_digest, "diff": diff,
        "documents": [{"document_id": d.document_id, "path": d.path, "side": d.side, "lines": d.line_count} for d in c.documents],
        "reports": [r.model_dump(mode="json") for r in store.records(run_id, "report").values() if r.candidate_id == candidate_id],
        "reviews": [r.model_dump(mode="json") for r in store.records(run_id, "review").values() if r.candidate_id == candidate_id],
        "is_final": store.get(run_id).get("final_candidate_id") == candidate_id,
        "note": "本版本测试/审查只属于此候选；模型意见未经独立裁决。旧摘要extended_passed仅指相对基线接受，不能当全量全绿；完整测试见运行级评分报告。"}


def evidence_view(store, run_id, evidence_id):
    ref = store.records(run_id, "evidence")[evidence_id]
    snapshot = store.snapshot(run_id, ref.candidate_id)
    if ref.document_id:
        raw = snapshot.read(ref.document_id)
        lines = raw.decode("utf-8").splitlines(keepends=True)
        text = "".join(lines[ref.start_line - 1:ref.end_line])
        if sha256(text.encode()) != ref.span_sha256:
            raise ProtocolError("evidence_span_hash_mismatch")
    else:
        report = store.records(run_id, "report")[ref.report_id]
        if sha256(canonical_bytes(report)) != ref.report_sha256:
            raise ProtocolError("evidence_report_hash_mismatch")
        text = canonical_bytes(report).decode()
    return {"evidence": ref.model_dump(mode="json"), "content": text}


def registered_artifact(store, row, name):
    path = Path(row["artifacts"][name])
    # Resolve only after checking each original component for reparse/links.
    for base in (ROOT / "experiments", store.home / "runs"):
        if path.is_relative_to(base):
            raw = checked_bytes(base, path.relative_to(base).as_posix())
            manifest = path.parent / "artifact_sha256.json"
            if row["kind"] in {"collaboration_live", "collaboration_mock"}:
                manifest = store.home / "runs" / row["id"] / "artifact_sha256.json"
                hashes = json.loads(_read(manifest.parent, manifest.name))
                if hashes.get(path.relative_to(manifest.parent).as_posix()) != sha256(raw):
                    raise ProtocolError("artifact_hash_mismatch")
            elif row["kind"] == "collaboration_history":
                archive = ROOT / "experiments/multi_agent_stage_c_live_20261023"
                hashes = json.loads(_read(archive, "artifact_sha256.json"))
                if not path.is_relative_to(archive) or hashes.get(path.relative_to(archive).as_posix()) != sha256(raw):
                    raise ProtocolError("archived_artifact_hash_mismatch")
            return raw
    raise ValueError("Artifact outside registered result directories")


def import_sealed_collaboration(store):
    """Index the known C acceptance as read-only history; never resume it."""
    import sqlite3
    archive = ROOT / "experiments/multi_agent_stage_c_live_20261023"
    if not archive.exists():
        return
    hashes = json.loads(_read(archive, "artifact_sha256.json"))
    # Use the archived key list, never discover arbitrary private input paths.
    for name, expected in hashes.items():
        if sha256(checked_bytes(archive, name)) != expected:
            raise ProtocolError("archived_collaboration_hash_mismatch")
    with sqlite3.connect((archive / "store/runs.sqlite3").as_uri() + "?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        record = json.loads(source.execute("SELECT payload FROM runs WHERE id=?", ("4891bcbc29c9439cb41439f13a203f3a",)).fetchone()[0])
        run_id = record["id"]
        with store.connect() as db:
            if db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                return
        objects = source.execute("SELECT * FROM collaboration_objects WHERE run_id=?", (run_id,)).fetchall()
        for entry in objects:
            if entry["kind"] != "snapshot":
                continue
            payload = json.loads(entry["payload"])
            relative = payload["relative_directory"]
            original, target = archive / "store" / relative, store.home / relative
            if not target.is_relative_to(store.home / "snapshots"):
                raise ProtocolError("archived_snapshot_outside_registered_root")
            for path in original.rglob("*"):
                if path.is_file():
                    raw = _read(archive, path.relative_to(archive).as_posix())
                    output = target / path.relative_to(original)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    if output.exists() and output.read_bytes() != raw:
                        raise ProtocolError("archived_snapshot_target_conflict")
                    if not output.exists():
                        output.write_bytes(raw)
        record.update(kind="collaboration_history", source="experiments/multi_agent_stage_c_live_20261023", reason=record["reason"] + "；只读归档，不恢复或重发。")
        record["artifacts"] = {name: str(archive / "run" / name) for name in ("summary.json", "repair.diff", "reviews.json", "reports.json", "patch-replay/report.json", "reviewing-trajectory.json") if "run/" + name in hashes}
        with store.transaction() as db:
            db.execute("INSERT INTO runs VALUES(?,NULL,?,?,?)", (run_id, record["status"], record["created"], canonical_bytes(record).decode()))
            meta = source.execute("SELECT * FROM collaboration_runs WHERE run_id=?", (run_id,)).fetchone()
            db.execute("INSERT INTO collaboration_runs VALUES(?,?,?,?,?,?,?)", tuple(meta))
            for role in source.execute("SELECT * FROM role_runs WHERE run_id=?", (run_id,)):
                db.execute("INSERT INTO role_runs VALUES(?,?,?,?,?)", tuple(role))
            for entry in objects:
                db.execute("INSERT INTO collaboration_objects VALUES(?,?,?,?,?,?)", tuple(entry))
            for event in source.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,)):
                store.emit(db, run_id, event["kind"], {"archived_at": event["at"], **json.loads(event["payload"])}, event["actor"])
            store.emit(db, run_id, "collaboration_history_imported", {"source": record["source"], "note": "Read-only C acceptance; old reports retain their original semantics."})
