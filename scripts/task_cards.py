"""Opt-in human task plans and versioned inputs. No model client or resume."""
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from task_service_store import now, worker_lock

FIELDS = {"schema_version", "goal", "constraints", "steps", "blocker", "next_action", "instruction"}
STATUSES = {"pending", "in_progress", "blocked", "done"}
TABLES = {"task_cards", "task_card_versions", "task_card_inputs"}


class TaskCardConflict(ValueError):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def validate_card(value):
    if not isinstance(value, dict) or set(value) - FIELDS:
        raise ValueError("任务卡包含不支持的字段。")
    if type(value.get("schema_version", 1)) is not int or value.get("schema_version", 1) != 1:
        raise ValueError("请刷新任务卡版本。")
    result = {"schema_version": 1}
    for key, limit in (("goal", 300), ("constraints", 300), ("blocker", 180), ("next_action", 180), ("instruction", 400)):
        text = value.get(key, "")
        if not isinstance(text, str) or len(text) > limit or any(ord(c) < 32 and c not in "\n\t\r" for c in text):
            raise ValueError("任务卡文字过长或包含不可用字符。")
        result[key] = text.strip()
    if not result["goal"]:
        raise ValueError("请填写希望完成的目标。")
    steps = value.get("steps", [])
    if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
        raise ValueError("请填写1至8个计划步骤。")
    result["steps"] = []
    for item in steps:
        if not isinstance(item, dict) or set(item) != {"id", "title", "status"}:
            raise ValueError("计划步骤格式不正确。")
        key, title, status = item["id"], item["title"], item["status"]
        if (not isinstance(key, str) or not 1 <= len(key) <= 48 or not all(c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in key)
                or not isinstance(title, str) or not title.strip() or len(title) > 100
                or any(ord(c) < 32 for c in title) or not isinstance(status, str) or status not in STATUSES):
            raise ValueError("请使用简短的步骤名称和有效状态。")
        result["steps"].append({"id": key, "title": title.strip(), "status": status})
    if len({step["id"] for step in result["steps"]}) != len(steps):
        raise ValueError("计划步骤编号不能重复。")
    if len(encoded(result)) > 2048:
        raise ValueError("任务卡整体过长，请缩短目标、步骤或补充要求（上限2048字节）。")
    return result


def has_schema(db):
    found = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if found & TABLES and not TABLES <= found:
        raise TaskCardConflict("任务卡存储不完整，请先核对，不自动重建。")
    return TABLES <= found


def ensure_schema(store):
    with worker_lock(store.home, "task_cards_migration.lock") as acquired:
        if not acquired:
            raise TaskCardConflict("任务卡正在初始化，请稍后刷新。")
        with store.connect() as db:
            if has_schema(db):
                return
            backup = store.home / ("before_task_cards_" + uuid.uuid4().hex + ".sqlite3")
            with sqlite3.connect(backup) as target:
                db.backup(target)
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE task_cards(run_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE task_card_versions(run_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY(run_id,revision));
                CREATE TABLE task_card_inputs(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, revision INTEGER NOT NULL, receiver TEXT NOT NULL, phase TEXT NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL, created TEXT NOT NULL, finished TEXT);
                CREATE INDEX task_card_inputs_run ON task_card_inputs(run_id,created);
                COMMIT;
            """)


def creation_fields(card):
    return {} if card is None else {"task_card_version": 1, "task_card_seed_sha256": digest(card)}


def insert_card(store, db, run_id, card):
    stamp, raw, checksum = now(), encoded(card).decode(), digest(card)
    db.execute("INSERT INTO task_cards VALUES(?,1,?,?,?)", (run_id, raw, checksum, stamp))
    db.execute("INSERT INTO task_card_versions VALUES(?,1,?,?,?)", (run_id, raw, checksum, stamp))
    store.emit(db, run_id, "task_card_created", {"revision": 1, "sha256": checksum, "scope": "Human plan; not independent verification"}, "user")


def can_edit(row):
    return (row.get("task_card_version") == 1 and row["kind"] in {"live", "collaboration_live", "collaboration_mock"}
            and row["status"] in {"queued", "running"} and not row["cancel_requested"])


def checked_card(db, run_id):
    row = db.execute("SELECT * FROM task_cards WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise TaskCardConflict("已启用任务卡但未找到对应记录，请核对原任务。")
    value = json.loads(row["payload"])
    if digest(value) != row["sha256"] or validate_card(value) != value:
        raise TaskCardConflict("任务卡内容校验失败。")
    version = db.execute("SELECT payload,sha256 FROM task_card_versions WHERE run_id=? AND revision=?", (run_id, row["revision"])).fetchone()
    if version is None or version["payload"] != row["payload"] or version["sha256"] != row["sha256"]:
        raise TaskCardConflict("任务卡版本校验失败。")
    return row, value


def card_view(store, run_id):
    parent = store.get(run_id)
    if parent.get("task_card_version") != 1:
        return {"enabled": False, "can_edit": False, "note": "此任务未开启互动任务卡；旧运行不补造计划。"}
    with store.connect() as db:
        if not has_schema(db):
            raise TaskCardConflict("缺少已登记的任务卡存储。")
        row, value = checked_card(db, run_id)
        versions = [dict(v) for v in db.execute("SELECT revision,sha256,created FROM task_card_versions WHERE run_id=? ORDER BY revision DESC LIMIT 20", (run_id,))]
        inputs = [dict(v) for v in db.execute("SELECT revision,receiver,phase,status,attempts,created,finished FROM task_card_inputs WHERE run_id=? ORDER BY rowid DESC LIMIT 20", (run_id,))]
    return {"enabled": True, "can_edit": can_edit(parent), "revision": row["revision"], "sha256": row["sha256"],
        "card": value, "versions": versions, "inputs": inputs, "runtime_stage": parent["stage"], "runtime_status": parent["status"],
        "note": "步骤状态由用户维护，不能代替独立验收。每次模型调用读取当时最新整张任务卡；有响应不证明模型遵循要求。暂停与续做能力见阶段暂停区域，旧任务不自动恢复。"}


def update_card(store, run_id, body):
    if not isinstance(body, dict) or set(body) != {"revision", "card"} or type(body["revision"]) is not int:
        raise ValueError("请携带当前任务卡版本再保存。")
    value = validate_card(body["card"])
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        raw = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
        if raw is None:
            raise KeyError("Run does not exist")
        if not can_edit(json.loads(raw[0])):
            raise ValueError("只能修改已开启任务卡且仍在排队或运行的任务；历史、结束或取消中的任务只读。")
        row, _ = checked_card(db, run_id)
        if row["revision"] != body["revision"]:
            raise TaskCardConflict("任务卡已被其他页面修改。请先刷新，核对后再保存；本次未覆盖。")
        if digest(value) == row["sha256"]:
            return card_view(store, run_id)
        revision, stamp, checksum = row["revision"] + 1, now(), digest(value)
        db.execute("UPDATE task_cards SET revision=?,payload=?,sha256=?,updated=? WHERE run_id=?", (revision, encoded(value).decode(), checksum, stamp, run_id))
        db.execute("INSERT INTO task_card_versions VALUES(?,?,?,?,?)", (run_id, revision, encoded(value).decode(), checksum, stamp))
        store.emit(db, run_id, "task_card_updated", {"revision": revision, "sha256": checksum, "previous_revision": row["revision"]}, "user")
    return card_view(store, run_id)


def begin_input(store, run_id, receiver, phase):
    if store.get(run_id).get("task_card_version") != 1:
        return None
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        parent = json.loads(db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()[0])
        if parent["status"] != "running" or parent["cancel_requested"]:
            raise TaskCardConflict("当前任务已停止，不能准备新的任务卡输入。")
        row, card = checked_card(db, run_id)
        key = uuid.uuid4().hex
        payload = {"task_card_input_id": key, "task_card_revision": row["revision"], "task_card_sha256": row["sha256"], "human_task_card": card,
            "scope": "Current human plan replaces earlier card views. Apply only within your current role. The frozen original issue, write/read restrictions, tool capabilities, deadlines and program verification remain authoritative. Step status is human-reported, not test truth. Do not expand your role or claim independent verification from this card."}
        message = {"role": "user", "content": encoded(payload).decode()}
        if len(encoded(message)) > 4096:
            raise ValueError("任务卡请求视图超过保护。")
        db.execute("INSERT INTO task_card_inputs VALUES(?,?,?,?,?,?,?,0,?,NULL)", (key, run_id, row["revision"], receiver, phase,
            hashlib.sha256(encoded(message)).hexdigest(), "prepared", now()))
        store.emit(db, run_id, "task_card_input_prepared", {"input_id": key, "revision": row["revision"], "receiver": receiver, "phase": phase})
    return {"id": key, "message": message, "attempts": 0, "response": False}


def finish_input(store, run_id, receipt):
    status = "response_received" if receipt["response"] else "outcome_unknown" if receipt["attempts"] else "not_dispatched"
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE task_card_inputs SET status=?,attempts=?,finished=? WHERE id=? AND run_id=?", (status, receipt["attempts"], now(), receipt["id"], run_id))
        store.emit(db, run_id, "task_card_input_finished", {"input_id": receipt["id"], "status": status, "attempts": receipt["attempts"]})


class TaskCardModelMixin:
    """Project an opt-in card only on the wire; original trajectory is intact."""
    def query(self, messages):
        store, run_id, receiver, phase = self.task_card_source()
        receipt = begin_input(store, run_id, receiver, phase)
        self._task_card_receipt = receipt
        try:
            if receipt and self.contains_credentials(receipt["message"]["content"]):
                raise ValueError("task_card_credential_blocked")
            return super().query(messages)
        finally:
            self._task_card_receipt = None
            if receipt:
                # Outside the transport/fallback handler: a storage failure must
                # never be mistaken for provider failure and resend a paid call.
                finish_input(store, run_id, receipt)

    def _prepare_messages_for_api(self, messages):
        prepared = super()._prepare_messages_for_api(messages)
        receipt = getattr(self, "_task_card_receipt", None)
        return prepared + [receipt["message"]] if receipt else prepared

    def _perform_completion(self, messages, provider):
        receipt = getattr(self, "_task_card_receipt", None)
        if receipt:
            receipt["attempts"] += 1
        response = super()._perform_completion(messages, provider)
        if receipt:
            receipt["response"] = True
        return response


def export_card(store, run_id, output):
    if store.get(run_id).get("task_card_version") != 1:
        return None
    view = card_view(store, run_id)
    with store.connect() as db:
        view["all_versions"] = [dict(row) for row in db.execute("SELECT * FROM task_card_versions WHERE run_id=? ORDER BY revision", (run_id,))]
        view["all_inputs"] = [dict(row) for row in db.execute("SELECT * FROM task_card_inputs WHERE run_id=? ORDER BY rowid", (run_id,))]
    path = Path(output) / "task-card.json"
    path.write_bytes(encoded(view))
    return path
