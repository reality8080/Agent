"""Durable SQLite checkpointer for LangGraph, including pending writes."""
from __future__ import annotations

import os
import sqlite3
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=4)
def get_checkpointer(db_path: str | None = None):
    """Use the official SQLite saver; never silently fall back to process memory."""
    path = str(Path(db_path or os.getenv("CHECKPOINTER_DB", "data/agent_checkpointer.db")).expanduser().resolve())
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    try:
        if os.name != "nt":
            Path(path).parent.chmod(0o700)
    except OSError:
        pass
    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError as exc:
        raise RuntimeError(
            "SQLite persistence requires langgraph-checkpoint-sqlite. "
            "Install src/adapters/requirements-adapters.txt; in-memory fallback is disabled."
        ) from exc
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    saver = SqliteSaver(conn)
    saver.setup()
    try:
        if os.name != "nt":
            Path(path).chmod(0o600)
    except OSError:
        pass
    return saver
