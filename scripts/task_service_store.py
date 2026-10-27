"""Durable local runs, event records and read-only experiment discovery."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOME = ROOT / "data/task_service"
TERMINAL = {"finished", "failed", "cancelled", "interrupted", "paused"}
PROFILES = {"raw": "原版适配", "adaptive_history": "上下文与任务状态", "retrieval_on_change": "上下文＋代码检索（按变化发送，实验选项）"}


def now():
    return datetime.now(timezone.utc).isoformat()


def catalog():
    rows = []
    for path in sorted((ROOT / "tasks").glob("*/manifest.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if "task_files_sha256" in data and data.get("language", "python") == "python":
            rows.append({"id": path.parent.name, "task_id": data["task_id"], "scope": data["scope"], "language": "python"})
    from comparison_inputs import catalog_rows
    rows.extend(catalog_rows())
    return rows


def policy(profile):
    if profile not in PROFILES:
        raise ValueError("Unknown strategy")
    if profile == "raw":
        return {}
    base = json.loads((ROOT / "configs/retrieval_delivery_trial_20261015.json").read_text(encoding="utf-8"))["variants"]["retrieval_on_change"]
    if profile == "adaptive_history":
        base.pop("repository_retrieval")
    return base


@contextmanager
def worker_lock(home, name="worker.lock"):
    """OS releases the lock after a crash. Never delete the lock or a claim."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    with (home / name).open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        try:
            yield acquired
        finally:
            if acquired:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class RunStore:
    def __init__(self, home=DEFAULT_HOME):
        self.home = Path(home).resolve()
        if not any(self.home.is_relative_to((ROOT / name).resolve()) for name in ("data", "temp")):
            raise ValueError("Service storage must be inside project data/ or temp/")
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "runs.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, request_key TEXT UNIQUE, status TEXT NOT NULL,
                    created TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    at TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL);
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def emit(db, run_id, kind, data, actor="system"):
        db.execute("INSERT INTO events(run_id,at,kind,actor,payload) VALUES(?,?,?,?,?)",
                   (run_id, now(), kind, actor, json.dumps(data, ensure_ascii=False)))

    def create(self, task, profile, mode, request_key, *, task_card=None, model_profile=None):
        from model_profiles import selection_fields
        model_fields = selection_fields(model_profile,execution=mode)
        if task not in {item["id"] for item in catalog()} or profile not in PROFILES or mode not in {"live", "replay"}:
            raise ValueError("Select a supported fixed task, strategy and mode")
        if mode == "replay" and task not in {"click-custom-command", "barrel-sort"}:
            raise ValueError("Offline replay supports the two round-19 tasks")
        if mode == "replay" and profile != "retrieval_on_change":
            raise ValueError("Offline replay uses the archived on_change patch, not a new strategy run")
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 120:
            raise ValueError("A bounded idempotency key is required")
        from task_cards import validate_card, ensure_schema, creation_fields, insert_card
        card = validate_card(task_card) if task_card is not None else None
        if card is not None:
            if mode != "live":
                raise ValueError("互动任务卡仅用于新建真实任务；离线重放保持原样。")
            ensure_schema(self)
        record = {"id": uuid.uuid4().hex, "kind": mode, "task": task, "profile": profile,
                  "status": "queued", "stage": "queued", "created": now(), "updated": now(),
                  "agent_exit": None, "repair_result": "not_run", "cancel_requested": False,
                  "metrics": {}, "artifacts": {}, "scope": next(t["scope"] for t in catalog() if t["id"] == task),
                  "configuration": policy(profile), "reason": "", "actor": "single_agent"}
        record.update(creation_fields(card))
        record.update(model_fields)
        if card is not None:
            record["scope"] += " Interactive human task card enabled; not a frozen baseline comparison."
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            found = db.execute("SELECT payload FROM runs WHERE request_key=?", (request_key,)).fetchone()
            if found:
                old = json.loads(found[0])
                if (old["task"], old["profile"], old["kind"], old.get("task_card_seed_sha256"),old.get('model_profile_sha256')) != (task, profile, mode, record.get("task_card_seed_sha256"),record.get('model_profile_sha256')):
                    raise ValueError("Idempotency key belongs to a different request")
                return old
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?)", (record["id"], request_key, "queued", record["created"], json.dumps(record, ensure_ascii=False)))
            self.emit(db, record["id"], "created", {"mode": mode, "profile": profile})
            if card is not None:
                insert_card(self, db, record["id"], card)
        return record

    def get(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError("Run does not exist")
        return json.loads(row[0])

    def list(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM runs ORDER BY created DESC,id DESC")]

    def update(self, run_id, *, event="updated", actor="system", expected=None, **fields):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError("Run does not exist")
            record = json.loads(row[0])
            if record["kind"] == "history":
                raise ValueError("Historical evidence is read-only")
            if record["status"] in TERMINAL or expected is not None and record["status"] not in expected:
                return None
            if fields.get("status") == "finished" and record["cancel_requested"]:
                fields = fields | {"status": "cancelled", "reason": "取消请求到达时运行正在收尾；已有验收结果保留。"}
            record.update(fields, updated=now())
            db.execute("UPDATE runs SET status=?,payload=? WHERE id=?", (record["status"], json.dumps(record, ensure_ascii=False), run_id))
            self.emit(db, run_id, event, fields, actor)
        return record

    def event(self, run_id, kind, data, actor="system"):
        with self.connect() as db:
            self.emit(db, run_id, kind, data, actor)

    def events(self, run_id, after=0):
        self.get(run_id)
        with self.connect() as db:
            return [{"seq": row["seq"], "at": row["at"], "kind": row["kind"], "actor": row["actor"], "data": json.loads(row["payload"])}
                    for row in db.execute("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 500", (run_id, after))]

    def cancel(self, run_id):
        record = self.get(run_id)
        if record["kind"] == "history":
            raise ValueError("Historical evidence is read-only")
        if record["status"] in TERMINAL:
            return record
        # Atomic condition prevents cancelling a claimed run as if it were queued.
        result = self.update(run_id, event="cancelled_before_start", expected={"queued"}, status="cancelled", cancel_requested=True, ended=now())
        return result or self.update(run_id, event="cancel_requested", expected={"running", "cancel_requested"}, status="cancel_requested", cancel_requested=True) or self.get(run_id)

    def reconcile(self):
        """Call only while holding worker_lock. Do not resume or repeat paid work."""
        for row in self.list():
            if row["kind"] != "history" and row["status"] in {"running", "cancel_requested"}:
                self.update(row["id"], event="worker_interrupted", status="interrupted", stage="interrupted", ended=now(), reason="执行进程已退出；保留证据，不自动续跑或重发请求。", repair_result="unknown")


def history_record(path):
    """Normalize fields without fabricating missing values or changing the source."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if "task_id" not in data or not any(key in data for key in ("repair_verified", "exit_status")):
        return None
    relative = path.relative_to(ROOT).as_posix()
    usage = data.get("usage", [])
    artifacts = {name: str(path.parent / name) for name in ("summary.json", "repair.diff", "repair_lf.diff", "trajectory.json", "verification.json", "patch_verification.json", "run_config.json", "baseline.json") if (path.parent / name).is_file()}
    requests = data.get("requests", data.get("external_model_requests"))
    known = data.get("known_price_estimate_cny", data.get("estimated_cost_cny"))
    fixed = data.get("repair_verified")
    date = re.search(r"(20\d{2})(\d{2})(\d{2})", relative)
    created = "-".join(date.groups()) + "T00:00:00+00:00" if date else "1970-01-01T00:00:00+00:00"
    return {"id": "history-" + hashlib.sha256(relative.encode()).hexdigest()[:24], "kind": "history", "normalizer_version": 2,
            "task": data["task_id"], "profile": data.get("variant", "historical"), "status": "finished",
            "stage": "historical", "created": created, "updated": None,
            "agent_exit": data.get("exit_status"), "repair_result": "passed" if fixed is True else "failed" if fixed is False else "unknown",
            "cancel_requested": False, "scope": data.get("scope", "见原始报告，范围未统一"), "source": relative,
            "reason": ("本次结果曾因补丁换行收集失败而离线恢复，缺失耗时保持未知；未重跑模型。" if data.get("collection_recovered_offline") else "历史记录只读；缺失字段保留未知，结束不等于修复通过。"), "artifacts": artifacts,
            "metrics": {"requests": requests, "successful_responses": len(usage) if usage else None,
                        "prompt_tokens": sum(x["prompt_tokens"] for x in usage) if usage else None,
                        "completion_tokens": sum(x["completion_tokens"] for x in usage) if usage else None,
                        "known_price_estimate_cny": known, "complete_cost_estimate_cny": data.get("estimated_cost_cny"),
                        "elapsed_agent_seconds": data.get("elapsed_seconds"), "cost_scope": "历史口径；完整费用可能未知，见原始报告"}}


def import_history(store):
    count = 0
    for path in (ROOT / "experiments").rglob("summary.json"):
        record = history_record(path)
        if record is None:
            continue
        with store.connect() as db:
            cursor = db.execute("INSERT OR IGNORE INTO runs VALUES(?,?,?,?,?)", (record["id"], None, "finished", record["created"], json.dumps(record, ensure_ascii=False)))
            if cursor.rowcount:
                store.emit(db, record["id"], "history_imported", {"source": record["source"]})
                count += 1
            else:
                old = json.loads(db.execute("SELECT payload FROM runs WHERE id=?", (record["id"],)).fetchone()[0])
                if old.get("normalizer_version", 1) < 2:
                    db.execute("UPDATE runs SET payload=? WHERE id=?", (json.dumps(record, ensure_ascii=False), record["id"]))
            marker = "history_actions_indexed"
            if not db.execute("SELECT 1 FROM events WHERE run_id=? AND kind=?", (record["id"], marker)).fetchone():
                trace = path.parent / "trajectory.json"
                if trace.is_file():
                    messages = json.loads(trace.read_text(encoding="utf-8")).get("messages", [])
                    for message in messages:
                        for action in message.get("extra", {}).get("actions", []):
                            if isinstance(action.get("command"), str):
                                store.emit(db, record["id"], "historical_action", {"command": action["command"], "source": "trajectory.json"}, "repairer")
                store.emit(db, record["id"], marker, {"note": "历史动作来自原轨迹；事件索引时间不等于动作原发生时间"})
    return count
