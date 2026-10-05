import json
import sqlite3
from datetime import datetime, timezone

from . import rules
from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    external_id TEXT,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, stable_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, source_type, external_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            # 迁移：为已存在的 items 表补充 external_id 列
            columns = [row[1] for row in conn.execute("PRAGMA table_info(items)").fetchall()]
            if "external_id" not in columns:
                conn.execute("ALTER TABLE items ADD COLUMN external_id TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_items_external_id ON items(entity_type, external_id)"
            )
        finally:
            conn.close()

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload):
        previous = self._last_hash(conn, item_id)
        event = {
            "item_id": item_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
        )

    def _apply_source(self, conn, item, source_type, external_id, source_payload, observed_at, actor, role):
        """在事务内插入来源记录、合并读数、重算等级并处理升级退回。

        调用方必须已开启 BEGIN IMMEDIATE 事务。返回来源记录信息。
        """
        item_id = item["id"]
        try:
            conn.execute(
                "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, source_type, external_id, canonical_json(source_payload), observed_at, now_iso()),
            )
        except sqlite3.IntegrityError:
            raise ConflictError("duplicate_source", "同一来源记录已经提交")
        source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

        item_payload = item["payload"]
        old_assessment = item_payload.get("assessment") or rules.assess(item_payload)
        old_level = old_assessment["level"]

        new_level = rules.apply_reading(item_payload, source_payload)

        escalation = None
        isolation_level = item_payload.get("isolation_level")
        if isolation_level and rules.level_rank(new_level) > rules.level_rank(isolation_level):
            if item["status"] == "isolated":
                item_payload["valve_sequence"] = []
                escalation = {
                    "source_id": source_id,
                    "external_id": external_id,
                    "source_type": source_type,
                    "observed_at": observed_at,
                    "from_level": isolation_level,
                    "to_level": new_level,
                }
                item_payload["last_escalation"] = escalation

        new_status = "verified" if escalation and item["status"] == "isolated" else item["status"]
        version = int(item["version"]) + 1
        conn.execute(
            "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
            (new_status, version, canonical_json(item_payload), now_iso(), item_id),
        )

        self.append_audit(
            conn,
            item_id,
            "source_recorded",
            actor,
            role,
            {
                "source_id": source_id,
                "source_type": source_type,
                "external_id": external_id,
                "observed_at": observed_at,
                "old_level": old_level,
                "new_level": new_level,
            },
        )
        if escalation:
            self.append_audit(conn, item_id, "level_escalated", actor, role, escalation)

        return {
            "id": source_id,
            "item_id": item_id,
            "source_type": source_type,
            "external_id": external_id,
            "payload": source_payload,
            "observed_at": observed_at,
            "escalation": escalation,
        }

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role):
        external_id = payload.get("external_id")
        source_type = payload.pop("source_type", None) or "manual"
        source_external_id = payload.pop("source_external_id", None) or external_id
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # 按外部编号合并：若已存在同一外部编号的管段，则把本次读数作为来源补交进去
            if external_id:
                row = conn.execute(
                    "SELECT * FROM items WHERE entity_type=? AND external_id=? ORDER BY id DESC LIMIT 1",
                    (entity_type, external_id),
                ).fetchone()
                if row is not None:
                    item = self._row_to_item(row)
                    source_payload = {
                        "sensor_value_ppm": payload.get("sensor_value_ppm"),
                        "pressure_drop_kpa": payload.get("pressure_drop_kpa"),
                        "odor_reports": payload.get("odor_reports", 0),
                        "note": payload.get("note", ""),
                    }
                    self._apply_source(
                        conn,
                        item,
                        source_type,
                        source_external_id,
                        source_payload,
                        payload.get("reported_at"),
                        actor,
                        role,
                    )
                    conn.execute("COMMIT")
                    return self.get_item(item["id"])
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,payload,external_id,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        initial_status,
                        1,
                        canonical_json(payload),
                        external_id,
                        actor,
                        role,
                        now_iso(),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_item", "同一业务实体已经存在")
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn, item_id, "created", actor, role, {"stable_key": stable_key, "external_id": external_id}
            )
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_item(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def find_item_by_external_id(self, external_id):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM items WHERE entity_type=? AND external_id=? ORDER BY id DESC LIMIT 1",
                (rules.ENTITY_TYPE, external_id),
            ).fetchone()
            return self._row_to_item(row)
        finally:
            conn.close()

    def list_items(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def add_source(self, item_id, source_type, external_id, payload, observed_at, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            item = self._row_to_item(row)
            result = self._apply_source(conn, item, source_type, external_id, payload, observed_at, actor, role)
            conn.execute("COMMIT")
            return result
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_sources(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM sources WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items()}
        finally:
            conn.close()
