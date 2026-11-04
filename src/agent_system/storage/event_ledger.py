"""Independent append-only SQLite event ledger with SHA-256 hash chaining."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

_SECRET_KEY = re.compile(r"(api.?key|token|secret|password|authorization|credential)", re.I)
_SECRET_TEXT = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*([^\s,;]+)")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if _SECRET_KEY.search(str(k)) else _redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET_TEXT.sub(r"\1=[REDACTED]", value)[:20000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:20000]


class EventLedger:
    """Append-only operational evidence, separate from LangGraph checkpoints."""

    def __init__(self, db_path: Optional[str] = None):
        raw = db_path or os.getenv("EVENT_LEDGER_DB", "data/agent_event_ledger.db")
        self.db_path = str(Path(raw).expanduser().resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            if os.name != "nt":
                Path(self.db_path).parent.chmod(0o700)
        except OSError:
            pass
        self._init_schema()
        try:
            if os.name != "nt":
                Path(self.db_path).chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                action_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                thread_id TEXT, task_id TEXT, run_id TEXT, trace_id TEXT,
                plan_version TEXT, scope_hash TEXT, environment_id TEXT, actor TEXT,
                approval TEXT, tool_result TEXT, exit_code INTEGER, artifact_refs TEXT,
                payload TEXT, created_at REAL NOT NULL,
                previous_event_hash TEXT, event_hash TEXT NOT NULL,
                UNIQUE(action_id, event_type)
            )""")
            cols = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
            additions = {
                "run_id": "TEXT", "trace_id": "TEXT", "approval": "TEXT",
                "tool_result": "TEXT", "exit_code": "INTEGER", "artifact_refs": "TEXT",
                "previous_event_hash": "TEXT", "event_hash": "TEXT NOT NULL DEFAULT ''",
            }
            for name, decl in additions.items():
                if name not in cols:
                    conn.execute(f"ALTER TABLE events ADD COLUMN {name} {decl}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_thread ON events(thread_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_action ON events(action_id)")
            conn.execute("CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'event ledger is append-only'); END")
            conn.execute("CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'event ledger is append-only'); END")
            # Backfill legacy rows with a verifiable chain in insertion order.
            rows = conn.execute("SELECT id,event_hash FROM events ORDER BY id").fetchall()
            if rows and any(not row["event_hash"] for row in rows):
                conn.execute("DROP TRIGGER IF EXISTS events_no_update")
                previous = ""
                for row in conn.execute("SELECT * FROM events ORDER BY id").fetchall():
                    if row["event_hash"]:
                        previous = row["event_hash"]
                        continue
                    record = {k: row[k] for k in row.keys() if k not in ("id", "event_hash", "previous_event_hash")}
                    digest = self._hash(record, previous)
                    conn.execute("UPDATE events SET previous_event_hash=?,event_hash=? WHERE id=?", (previous or None, digest, row["id"]))
                    previous = digest
                conn.execute("CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'event ledger is append-only'); END")
            conn.commit()

    @staticmethod
    def _hash(record: Dict[str, Any], previous_hash: str) -> str:
        blob = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        return hashlib.sha256((previous_hash + "\n" + blob).encode("utf-8")).hexdigest()

    def append(
        self, event_type: str, action_id: str, payload: Optional[Dict[str, Any]] = None,
        thread_id: Optional[str] = None, task_id: Optional[str] = None,
        plan_version: Optional[str] = None, scope_hash: Optional[str] = None,
        environment_id: Optional[str] = None, actor: str = "system", *,
        run_id: Optional[str] = None, trace_id: Optional[str] = None,
        approval: Optional[Dict[str, Any]] = None,
        tool_result: Optional[Dict[str, Any]] = None, exit_code: Optional[int] = None,
        artifact_refs: Optional[Sequence[str]] = None,
    ) -> str:
        safe_payload = _redact(payload or {})
        safe_approval = _redact(approval) if approval is not None else None
        safe_tool_result = _redact(tool_result) if tool_result is not None else None
        safe_artifacts = _redact(list(artifact_refs or []))
        now = time.time()
        event_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT event_id FROM events WHERE action_id=? AND event_type=?", (action_id, event_type)
            ).fetchone()
            if existing:
                conn.rollback()
                return existing["event_id"]
            prev = conn.execute("SELECT event_hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
            previous_hash = prev["event_hash"] if prev else ""
            record = {
                "event_id": event_id, "action_id": action_id, "event_type": event_type,
                "thread_id": thread_id, "task_id": task_id, "run_id": run_id, "trace_id": trace_id,
                "plan_version": plan_version, "scope_hash": scope_hash, "environment_id": environment_id,
                "actor": actor,
                "approval": json.dumps(safe_approval, sort_keys=True) if safe_approval is not None else None,
                "tool_result": json.dumps(safe_tool_result, sort_keys=True) if safe_tool_result is not None else None,
                "exit_code": exit_code,
                "artifact_refs": json.dumps(safe_artifacts, sort_keys=True),
                "payload": json.dumps(safe_payload, sort_keys=True, ensure_ascii=False),
                "created_at": now,
            }
            digest = self._hash(record, previous_hash)
            conn.execute("""INSERT INTO events (
                event_id,action_id,event_type,thread_id,task_id,run_id,trace_id,
                plan_version,scope_hash,environment_id,actor,approval,tool_result,exit_code,
                artifact_refs,payload,created_at,previous_event_hash,event_hash
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                record["event_id"], record["action_id"], record["event_type"], record["thread_id"],
                record["task_id"], record["run_id"], record["trace_id"], record["plan_version"],
                record["scope_hash"], record["environment_id"], record["actor"], record["approval"],
                record["tool_result"], record["exit_code"], record["artifact_refs"], record["payload"],
                record["created_at"], previous_hash or None, digest,
            ))
            conn.commit()
        return event_id

    def list_for_thread(self, thread_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM events WHERE thread_id=? ORDER BY id ASC LIMIT ?", (thread_id, limit)).fetchall()
            return [dict(r) for r in rows]

    def get_by_action(self, action_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM events WHERE action_id=? ORDER BY id ASC", (action_id,)).fetchall()
            return [dict(r) for r in rows]

    def verify_integrity(self) -> Dict[str, Any]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY id ASC").fetchall()
        previous = ""
        for row in rows:
            record = {k: row[k] for k in row.keys() if k not in ("id", "event_hash", "previous_event_hash")}
            expected_prev = previous or None
            if row["previous_event_hash"] != expected_prev:
                return {"ok": False, "events": len(rows), "bad_event_id": row["event_id"], "reason": "previous_hash_mismatch"}
            expected = self._hash(record, previous)
            if not __import__("hmac").compare_digest(row["event_hash"], expected):
                return {"ok": False, "events": len(rows), "bad_event_id": row["event_id"], "reason": "event_hash_mismatch"}
            previous = row["event_hash"]
        return {"ok": True, "events": len(rows), "head_hash": previous or None}
