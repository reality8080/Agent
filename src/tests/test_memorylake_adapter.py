"""
Unit / mock tests for MemoryLakeAdapter.

  MEMORYLAKE_MOCK=1 python src/tests/test_memorylake_adapter.py

Token is optional (MCP clients use OAuth). Mock only when MEMORYLAKE_MOCK=1.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_CANDIDATE_ROOTS = [
    _HERE.parent,
    _HERE.parent.parent,
    Path.cwd(),
    Path.cwd().parent,
]

ROOT = Path.cwd()
for _r in _CANDIDATE_ROOTS:
    if (_r / "adapters").is_dir() or (_r / "src" / "adapters").is_dir():
        ROOT = _r
        break

for _p in (ROOT, ROOT / "src"):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    from dotenv import load_dotenv

    for _base in list(dict.fromkeys(_CANDIDATE_ROOTS + [ROOT, Path.cwd()])):
        _env_path = _base / ".env"
        if _env_path.is_file():
            load_dotenv(_env_path, override=True)
    load_dotenv(override=False)
except ImportError:
    pass

from src.adapters.memorylake_adapter import (
    MemoryLakeAdapter,
    MemoryStatus,
    MemoryType,
    create_memorylake_adapter,
)


def test_mock_propose_and_search():
    adapter = MemoryLakeAdapter(mock=True, project_id="proj-demo")
    assert adapter.mock is True
    health = adapter.health()
    assert health["mock"] is True
    assert health["live_ready"] is False

    mem = adapter.propose_memory(
        content="Decision: use SQLite Event Ledger as append-only evidence store.",
        memory_type=MemoryType.FACT,
        project_id="proj-demo",
        task_id="task-001",
        source_ref="spec:v0.5#memory",
        evidence_ref=["event:abc"],
        created_by="agent:planner",
        confidence=0.85,
    )
    assert mem.memory_id
    assert mem.status == MemoryStatus.PROPOSED
    assert mem.scope.project_id == "proj-demo"
    assert mem.version == 1
    assert mem.remote_id and mem.remote_id.startswith("mock-")

    hits = adapter.search("Event Ledger", project_id="proj-demo")
    assert len(hits) >= 1
    assert hits[0].memory.memory_id == mem.memory_id
    assert "Event Ledger" in (hits[0].snippet or hits[0].memory.content)


def test_refuse_agent_self_promotion_on_write():
    adapter = MemoryLakeAdapter(mock=True, project_id="proj-demo")
    mem = adapter.propose_memory(
        content="Should stay proposed",
        memory_type=MemoryType.FACT,
        project_id="proj-demo",
        created_by="agent:coder",
        status=MemoryStatus.APPROVED,  # attempt
    )
    assert mem.status == MemoryStatus.PROPOSED


def test_promote_requires_privileged_actor():
    adapter = MemoryLakeAdapter(mock=True, project_id="proj-demo")
    mem = adapter.propose_memory(
        content="Promote me later",
        memory_type=MemoryType.REFLECTION,
        project_id="proj-demo",
        created_by="agent:reviewer",
    )
    try:
        adapter.promote(mem.memory_id, MemoryStatus.APPROVED, actor="agent:reviewer")
        assert False, "expected PermissionError"
    except PermissionError:
        pass

    promoted = adapter.promote(
        mem.memory_id,
        MemoryStatus.APPROVED,
        actor="user:owner",
        reason="Reviewed against acceptance criteria",
    )
    assert promoted.status == MemoryStatus.APPROVED
    assert promoted.version == 2


def test_project_scope_required():
    # Force no default project even if MEMORYLAKE_PROJECT_ID is set in the environment
    adapter = MemoryLakeAdapter(mock=True, project_id=None)
    adapter.default_project_id = None
    try:
        adapter.propose_memory(
            content="no project",
            memory_type=MemoryType.FACT,
            created_by="agent",
        )
        assert False, "expected ValueError for missing project_id"
    except ValueError as e:
        assert "project_id" in str(e)


def test_detect_conflicts_mock():
    adapter = MemoryLakeAdapter(mock=True, project_id="proj-demo")
    report = adapter.detect_conflicts(query="ledger")
    assert report.mock is True
    assert report.conflicts == []


def test_list_resources_mock():
    adapter = MemoryLakeAdapter(mock=True, project_id="proj-demo")
    adapter.propose_memory(
        content="A",
        memory_type=MemoryType.DIALOGUE,
        project_id="proj-demo",
        created_by="agent",
    )
    resources = adapter.list_resources()
    assert len(resources) >= 1
    assert resources[0]["uri"].startswith("memorylake://mock/")


def test_discover_tools_mock():
    adapter = MemoryLakeAdapter(mock=True)
    tools = adapter.discover_tools()
    assert "search_memory" in tools
    assert "add_memory" in tools
    assert "detect_conflicts" in tools


def test_factory():
    a = create_memorylake_adapter(mock=True, project_id="p1")
    assert a.mock is True
    assert a.default_project_id == "p1"


def test_default_is_live_without_token():
    """Token optional; mock only when MEMORYLAKE_MOCK=1."""
    prev = os.environ.pop("MEMORYLAKE_MOCK", None)
    try:
        adapter = MemoryLakeAdapter(mock=None)
        assert adapter.mock is False
        health = adapter.health()
        assert health["live_ready"] is True
        assert health["has_token"] is bool(adapter.token)
    finally:
        if prev is not None:
            os.environ["MEMORYLAKE_MOCK"] = prev


if __name__ == "__main__":
    test_mock_propose_and_search()
    test_refuse_agent_self_promotion_on_write()
    test_promote_requires_privileged_actor()
    test_project_scope_required()
    test_detect_conflicts_mock()
    test_list_resources_mock()
    test_discover_tools_mock()
    test_factory()
    test_default_is_live_without_token()
    print("All mock / unit tests passed.")