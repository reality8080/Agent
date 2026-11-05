"""
Unit / mock tests for MemoryLakeAdapter.

  MEMORYLAKE_MOCK=1 python src/tests/test_memorylake_adapter.py

Token is optional (MCP clients use OAuth). Mock only when MEMORYLAKE_MOCK=1.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import types
from pathlib import Path

import pytest

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
    MemoryLakeKeyringStorage,
    MemoryStatus,
    MemoryType,
    _LoopbackOAuthCallback,
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


def test_default_is_live_but_not_verified_without_mcp_operation(monkeypatch):
    """MOCK=0 means live mode was requested, not that OAuth/MCP is ready."""
    monkeypatch.delenv("MEMORYLAKE_MOCK", raising=False)
    adapter = MemoryLakeAdapter(mock=None, token="personal-rest-api-key")
    health = adapter.health()
    assert adapter.mock is False
    assert health["live_ready"] is False
    assert health["oauth_authenticated"] is False
    assert health["status"] == "oauth_required_or_unverified"
    assert health["has_legacy_token"] is True
    assert getattr(adapter, "token", None) is None


def test_live_discovery_failure_does_not_fallback_to_documented_names(monkeypatch):
    adapter = MemoryLakeAdapter(mock=False)

    def fail(_operation):
        raise RuntimeError("discovery unavailable")

    monkeypatch.setattr(adapter, "_run_live", fail)
    with pytest.raises(RuntimeError, match="discovery unavailable"):
        adapter.discover_tools()
    assert adapter._discovered_tools is None
    assert adapter.health()["live_ready"] is False


def test_live_discovery_uses_only_server_returned_tools(monkeypatch):
    adapter = MemoryLakeAdapter(mock=False)
    actual = (["search_memory"], {"search_memory": {"properties": {"query": {"type": "string"}}}})
    def succeed(_operation):
        adapter._live_verified = True
        adapter._oauth_authenticated = True
        return actual

    monkeypatch.setattr(adapter, "_run_live", succeed)
    assert adapter.discover_tools() == ["search_memory"]
    assert adapter.discovered_tool_schemas["search_memory"]["properties"].keys() == {"query"}
    assert adapter.health()["live_ready"] is True


def test_tool_arguments_are_filtered_to_discovered_schema():
    adapter = MemoryLakeAdapter(mock=False)
    result = adapter._filter_tool_arguments(
        "search_memory",
        {"query": "hello", "top_k": 5, "project_id": "project-a"},
        {"properties": {"query": {}, "project_id": {}}},
    )
    assert result == {"query": "hello", "project_id": "project-a"}


def test_tool_argument_mapping_fails_closed_on_missing_required_field():
    adapter = MemoryLakeAdapter(mock=False)
    with pytest.raises(RuntimeError, match="not mapped"):
        adapter._filter_tool_arguments(
            "search_memory", {"query": "hello"},
            {"properties": {"query": {}, "workspace_id": {}}, "required": ["workspace_id"]},
        )


def test_live_add_memory_refuses_schema_without_project_scope():
    adapter = MemoryLakeAdapter(mock=False)
    with pytest.raises(RuntimeError, match="refusing an unscoped write"):
        adapter._filter_tool_arguments(
            "add_memory", {"content": "test", "project_id": "project-a"},
            {"properties": {"content": {}}},
        )


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

def test_keyring_storage_persists_oauth_tokens_and_client_info(monkeypatch):
    records = {}
    backend = types.SimpleNamespace(priority=5)
    fake_keyring = types.ModuleType("keyring")
    fake_keyring.get_keyring = lambda: backend
    fake_keyring.get_password = lambda service, username: records.get((service, username))
    fake_keyring.set_password = lambda service, username, value: records.__setitem__((service, username), value)
    fake_keyring.delete_password = lambda service, username: records.pop((service, username), None)

    class FakeOAuthToken:
        def __init__(self, access_token, refresh_token=None, token_type="Bearer", expires_in=3600):
            self.access_token, self.refresh_token = access_token, refresh_token
            self.token_type, self.expires_in = token_type, expires_in
        def model_dump(self, **kwargs):
            return {"token_type": self.token_type, "expires_in": self.expires_in}
        def model_dump_json(self, **kwargs):
            return json.dumps({"client_id": "client-1"})
        @classmethod
        def model_validate(cls, data):
            return cls(**data)
        @classmethod
        def model_validate_json(cls, raw):
            return cls(**json.loads(raw))

    class FakeClientInfo:
        def __init__(self, client_id):
            self.client_id = client_id
        def model_dump_json(self, **kwargs):
            return json.dumps({"client_id": self.client_id})
        @classmethod
        def model_validate_json(cls, raw):
            return cls(**json.loads(raw))

    mcp = types.ModuleType("mcp")
    shared = types.ModuleType("mcp.shared")
    auth = types.ModuleType("mcp.shared.auth")
    auth.OAuthToken = FakeOAuthToken
    auth.OAuthClientInformationFull = FakeClientInfo
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)
    monkeypatch.setitem(sys.modules, "mcp", mcp)
    monkeypatch.setitem(sys.modules, "mcp.shared", shared)
    monkeypatch.setitem(sys.modules, "mcp.shared.auth", auth)

    storage = MemoryLakeKeyringStorage("https://example.test/mcp")
    token = FakeOAuthToken("access-secret", "refresh-secret")
    client_info = FakeClientInfo("client-1")
    asyncio.run(storage.set_tokens(token))
    asyncio.run(storage.set_client_info(client_info))
    restored_token = asyncio.run(storage.get_tokens())
    restored_client = asyncio.run(storage.get_client_info())

    assert restored_token.access_token == "access-secret"
    assert restored_token.refresh_token == "refresh-secret"
    assert restored_client.client_id == "client-1"
    assert records[(storage.SERVICE_NAME, f"{storage._username}:access_token")] == "access-secret"


def test_keyring_storage_refuses_insecure_backend(monkeypatch):
    fake_keyring = types.ModuleType("keyring")
    fake_keyring.get_keyring = lambda: types.SimpleNamespace(priority=0)
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)
    with pytest.raises(RuntimeError, match="No secure OS credential backend"):
        MemoryLakeKeyringStorage("https://example.test/mcp")


def test_oauth_loopback_callback_preserves_code_state_and_issuer(monkeypatch):
    class FakeAuthorizationCodeResult:
        def __init__(self, code, state, iss):
            self.code, self.state, self.iss = code, state, iss

    mcp = types.ModuleType("mcp")
    client = types.ModuleType("mcp.client")
    auth = types.ModuleType("mcp.client.auth")
    auth.AuthorizationCodeResult = FakeAuthorizationCodeResult
    monkeypatch.setitem(sys.modules, "mcp", mcp)
    monkeypatch.setitem(sys.modules, "mcp.client", client)
    monkeypatch.setitem(sys.modules, "mcp.client.auth", auth)

    async def exercise():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        callback = _LoopbackOAuthCallback(timeout=3)
        callback.port = port
        await callback.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET /oauth/callback?code=code-1&state=state-1&iss=https%3A%2F%2Fissuer.example HTTP/1.1\r\n"
                b"Host: localhost\r\n\r\n"
            )
            await writer.drain()
            response = await reader.read()
            result = await callback.wait_for_callback()
            assert b"200 OK" in response
            assert (result.code, result.state, result.iss) == (
                "code-1", "state-1", "https://issuer.example"
            )
        finally:
            await callback.close()

    asyncio.run(exercise())
