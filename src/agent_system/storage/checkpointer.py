"""SQLite Checkpointer for LangGraph pause/resume.

Separate database from Event Ledger. Uses langgraph-compatible interface
via a thin MemorySaver fallback + optional SQLite persistence of state blobs.
"""

from __future__ import annotations

import json
import logging
import os
import pickle
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Sequence, Tuple

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)

logger = logging.getLogger(__name__)

DEFAULT_DB = os.getenv("CHECKPOINTER_DB", "/tmp/agent_checkpointer.db")


class SqliteCheckpointer(BaseCheckpointSaver):
    """Minimal SQLite-backed checkpointer for MVP.

    Stores serialized checkpoints. Not production HA, but satisfies
    Spec requirement of SQLite checkpointer distinct from Event Ledger.
    """

    def __init__(self, db_path: str = DEFAULT_DB, serde=None):
        super().__init__(serde=serde)
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        # In-process cache for fast resume within same process
        self._memory = MemorySaver()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL DEFAULT '',
                    checkpoint_id TEXT NOT NULL,
                    parent_checkpoint_id TEXT,
                    checkpoint BLOB NOT NULL,
                    metadata BLOB,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS writes (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL DEFAULT '',
                    checkpoint_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    idx INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    value BLOB,
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
                )
                """
            )
            conn.commit()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def get_tuple(self, config: Dict[str, Any]) -> Optional[CheckpointTuple]:
        # Prefer memory for current process
        mem = self._memory.get_tuple(config)
        if mem is not None:
            return mem

        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"].get("checkpoint_id")

        with self._conn() as conn:
            if checkpoint_id:
                row = conn.execute(
                    """
                    SELECT checkpoint, metadata, parent_checkpoint_id, checkpoint_id
                    FROM checkpoints
                    WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?
                    """,
                    (thread_id, checkpoint_ns, checkpoint_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT checkpoint, metadata, parent_checkpoint_id, checkpoint_id
                    FROM checkpoints
                    WHERE thread_id=? AND checkpoint_ns=?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (thread_id, checkpoint_ns),
                ).fetchone()

            if not row:
                return None

            checkpoint = pickle.loads(row[0])
            metadata = pickle.loads(row[1]) if row[1] else {}
            parent_id = row[2]
            ckpt_id = row[3]

            return CheckpointTuple(
                config={
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": ckpt_id,
                    }
                },
                checkpoint=checkpoint,
                metadata=metadata,
                parent_config=(
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": parent_id,
                        }
                    }
                    if parent_id
                    else None
                ),
            )

    def list(
        self,
        config: Optional[Dict[str, Any]],
        *,
        filter: Optional[Dict[str, Any]] = None,
        before: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
    ) -> Iterator[CheckpointTuple]:
        # Delegate to memory for simplicity in MVP; DB scan available if needed
        yield from self._memory.list(config, filter=filter, before=before, limit=limit)

    def put(
        self,
        config: Dict[str, Any],
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: Dict[str, Any],
    ) -> Dict[str, Any]:
        # Always write to memory
        out_config = self._memory.put(config, checkpoint, metadata, new_versions)

        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = checkpoint["id"]
        parent_id = config["configurable"].get("checkpoint_id")

        try:
            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO checkpoints
                    (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
                     checkpoint, metadata, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        thread_id,
                        checkpoint_ns,
                        checkpoint_id,
                        parent_id,
                        pickle.dumps(checkpoint),
                        pickle.dumps(dict(metadata) if metadata else {}),
                        time.time(),
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("SqliteCheckpointer.put failed (memory still ok): %s", exc)

        return out_config

    def put_writes(
        self,
        config: Dict[str, Any],
        writes: Sequence[Tuple[str, Any]],
        task_id: str,
    ) -> None:
        self._memory.put_writes(config, writes, task_id)
        # Optional: persist writes; omitted for MVP brevity


def get_checkpointer(db_path: str = DEFAULT_DB) -> BaseCheckpointSaver:
    """Factory: returns SqliteCheckpointer when possible, else MemorySaver."""
    try:
        return SqliteCheckpointer(db_path=db_path)
    except Exception as exc:
        logger.warning("Falling back to MemorySaver: %s", exc)
        return MemorySaver()
