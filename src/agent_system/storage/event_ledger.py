"""SQLite Event Ledger — audit trail only (separate from checkpointer).

Never used as LangGraph checkpointer. Append-only with action_id + idempotency.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DB = os.getenv("EVENT_LEDGER_DB", "/tmp/agent_event_ledger.db")


class EventLedger:
    """Append-only audit log. Idempotent on (action_id, event_type)."""

    def __init__(self, db_path: str = DEFAULT_DB):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE NOT NULL,
                    action_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    thread_id TEXT,
                    task_id TEXT,
                    plan_version TEXT,
                    scope_hash TEXT,
                    environment_id TEXT,
                    actor TEXT,
                    payload TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE(action_id, event_type)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_thread ON events(thread_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_action ON events(action_id)"
            )
            conn.commit()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def append(
        self,
        event_type: str,
        action_id: str,
        payload: Optional[Dict[str, Any]] = None,
        thread_id: Optional[str] = None,
        task_id: Optional[str] = None,
        plan_version: Optional[str] = None,
        scope_hash: Optional[str] = None,
        environment_id: Optional[str] = None,
        actor: str = "system",
    ) -> str:
        """Insert event. Returns event_id. Idempotent on (action_id, event_type)."""
        event_id = str(uuid.uuid4())
        now = time.time()
        try:
            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT INTO events (
                        event_id, action_id, event_type, thread_id, task_id,
                        plan_version, scope_hash, environment_id, actor, payload, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        action_id,
                        event_type,
                        thread_id,
                        task_id,
                        plan_version,
                        scope_hash,
                        environment_id,
                        actor,
                        json.dumps(payload or {}, default=str),
                        now,
                    ),
                )
                conn.commit()
            logger.debug("EventLedger.append %s action=%s", event_type, action_id)
            return event_id
        except sqlite3.IntegrityError:
            # Already recorded — return existing
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT event_id FROM events WHERE action_id=? AND event_type=?",
                    (action_id, event_type),
                ).fetchone()
                return row["event_id"] if row else event_id

    def list_for_thread(self, thread_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM events WHERE thread_id=? ORDER BY created_at ASC LIMIT ?
                """,
                (thread_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_by_action(self, action_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE action_id=? ORDER BY created_at ASC",
                (action_id,),
            ).fetchall()
            return [dict(r) for r in rows]
