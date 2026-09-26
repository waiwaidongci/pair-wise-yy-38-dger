from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import (HANDOVER_DECISIONS, HANDOVER_STATES, ID_PREFIX, STATES,
                    return_review_state)


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS handovers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    outgoing_officer TEXT NOT NULL,
                    incoming_officer TEXT NOT NULL,
                    reservoir_level REAL NOT NULL,
                    personnel TEXT NOT NULL,
                    note TEXT,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','completed')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS handover_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    handover_id INTEGER NOT NULL
                        REFERENCES handovers(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    item_version INTEGER NOT NULL,
                    item_status TEXT NOT NULL,
                    decision TEXT CHECK(decision IS NULL OR decision IN ('accepted','returned')),
                    reason TEXT,
                    decided_by TEXT,
                    decided_at TEXT,
                    UNIQUE(handover_id, item_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_open_handover_item
                    ON handover_items(item_id) WHERE decision IS NULL;
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def create_handover(self, outgoing_officer: str, incoming_officer: str,
                        reservoir_level: float, personnel: List[str],
                        note: Optional[str], item_ids: List[int],
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            rows = self.conn.execute(
                f"SELECT id, status, version FROM items WHERE id IN ({','.join('?' for _ in item_ids)})",
                item_ids,
            ).fetchall()
            found = {int(row["id"]): row for row in rows}
            missing = [i for i in item_ids if i not in found]
            if missing:
                raise NotFoundError(f"指令不存在: {missing[0]}")
            cur = self.conn.execute(
                """INSERT INTO handovers(outgoing_officer, incoming_officer, reservoir_level,
                   personnel, note, status, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (outgoing_officer, incoming_officer, reservoir_level,
                 json.dumps(personnel, ensure_ascii=False), note, HANDOVER_STATES[0],
                 actor, now),
            )
            handover_id = int(cur.lastrowid)
            try:
                self.conn.executemany(
                    """INSERT INTO handover_items(handover_id, item_id, item_version, item_status)
                       VALUES(?,?,?,?)""",
                    [(handover_id, found[i]["id"], int(found[i]["version"]),
                      found[i]["status"]) for i in item_ids],
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("存在已登记但尚未确认交接的指令") from exc
        return self.get_handover(handover_id)

    def get_handover(self, handover_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM handovers WHERE id=?", (handover_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("交接单不存在")
        return dict(row)

    def list_handovers(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM handovers"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_handover_items(self, handover_id: int) -> List[Dict[str, Any]]:
        self.get_handover(handover_id)
        with self._lock:
            rows = self.conn.execute(
                """SELECT hi.*, i.title AS item_title, i.severity AS item_severity
                   FROM handover_items hi JOIN items i ON i.id=hi.item_id
                   WHERE hi.handover_id=? ORDER BY hi.id""",
                (handover_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_handover_item(self, handover_item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM handover_items WHERE id=?", (handover_item_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("交接明细不存在")
        return dict(row)

    def open_lock_for_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT hi.id AS handover_item_id, hi.handover_id, h.incoming_officer
                   FROM handover_items hi
                   JOIN handovers h ON h.id=hi.handover_id
                   WHERE hi.item_id=? AND hi.decision IS NULL AND h.status='open'""",
                (item_id,),
            ).fetchone()
        return dict(row) if row else None

    def open_locks_for_items(self, item_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        if not item_ids:
            return {}
        with self._lock:
            rows = self.conn.execute(
                f"""SELECT hi.item_id AS item_id, hi.id AS handover_item_id,
                           hi.handover_id, h.incoming_officer
                    FROM handover_items hi
                    JOIN handovers h ON h.id=hi.handover_id
                    WHERE hi.decision IS NULL AND h.status='open'
                      AND hi.item_id IN ({','.join('?' for _ in item_ids)})""",
                item_ids,
            ).fetchall()
        return {int(row["item_id"]): dict(row) for row in rows}

    def accept_handover_item(self, handover_item_id: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE handover_items SET decision='accepted', reason=NULL,
                   decided_by=?, decided_at=?
                   WHERE id=? AND decision IS NULL""",
                (actor, now, handover_item_id),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM handover_items WHERE id=?", (handover_item_id,)
                ).fetchone()
                if exists is None:
                    raise NotFoundError("交接明细不存在")
                raise ConflictError("该指令已确认，不能重复操作")
            row = self.conn.execute(
                "SELECT handover_id FROM handover_items WHERE id=?", (handover_item_id,)
            ).fetchone()
            handover_id = int(row["handover_id"])
            pending = self.conn.execute(
                "SELECT COUNT(*) AS n FROM handover_items WHERE handover_id=? AND decision IS NULL",
                (handover_id,),
            ).fetchone()
            if int(pending["n"]) == 0:
                self.conn.execute(
                    "UPDATE handovers SET status='completed', completed_at=? WHERE id=? AND status='open'",
                    (now, handover_id),
                )
        return self.get_handover_item(handover_item_id)

    def return_handover_item(self, handover_item_id: int, reason: str,
                             actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM handover_items WHERE id=?", (handover_item_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("交接明细不存在")
            if row["decision"] is not None:
                raise ConflictError("该指令已确认，不能重复操作")
            item_id = int(row["item_id"])
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (return_review_state(), now, item_id, int(row["item_version"])),
            )
            if cur.rowcount == 0:
                latest = self.conn.execute(
                    "SELECT version FROM items WHERE id=?", (item_id,)
                ).fetchone()
                if latest is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
            self.conn.execute(
                """UPDATE handover_items SET decision='returned', reason=?,
                   decided_by=?, decided_at=?, item_version=item_version+1,
                   item_status=? WHERE id=?""",
                (reason, actor, now, return_review_state(), handover_item_id),
            )
            pending = self.conn.execute(
                "SELECT COUNT(*) AS n FROM handover_items WHERE handover_id=? AND decision IS NULL",
                (int(row["handover_id"]),),
            ).fetchone()
            if int(pending["n"]) == 0:
                self.conn.execute(
                    "UPDATE handovers SET status='completed', completed_at=? WHERE id=? AND status='open'",
                    (now, int(row["handover_id"])),
                )
        return self.get_handover_item(handover_item_id)

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None,
                   entity_type: Optional[str] = None) -> List[Dict[str, Any]]:
        clauses = []
        params: List[Any] = []
        if entity_id is not None:
            clauses.append("entity_id=?")
            params.append(entity_id)
        if entity_type is not None:
            clauses.append("entity_type=?")
            params.append(entity_type)
        sql = "SELECT * FROM audit_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
