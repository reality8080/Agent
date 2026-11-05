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


def test_conflict_detection_is_not_claimed_when_tool_is_unavailable():
    adapter = MemoryLakeAdapter(mock=True, project_id="proj-demo")
    report = adapter.detect_conflicts(query="ledger")
    assert report.mock is True
    assert report.available is False
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


def test_discover_tools_mock_matches_live_tool_contract():
    adapter = MemoryLakeAdapter(mock=True)
    tools = adapter.discover_tools()
    assert {"inspect", "search", "remember", "forget"}.issubset(tools)
    assert not {"search_memory", "add_memory", "detect_conflicts"}.intersection(tools)


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


def test_live_discovery_does_not_claim_retrieval_ready(monkeypatch):
    adapter = MemoryLakeAdapter(mock=False)
    actual = (["inspect", "search"], {
        "inspect": {"properties": {}},
        "search": {"properties": {"queries": {"type": "array"}}, "required": ["queries"]},
    })
    def succeed(_operation):
        adapter._oauth_authenticated = True
        return actual

    monkeypatch.setattr(adapter, "_run_live", succeed)
    assert adapter.discover_tools() == ["inspect", "search"]
    assert adapter.discovered_tool_schemas["search"]["properties"].keys() == {"queries"}
    health = adapter.health()
    assert health["mcp_discovery_verified"] is True
    assert health["live_ready"] is False
    assert health["ready_for_retrieval"] is False


def test_tool_arguments_are_filtered_to_actual_search_schema():
    adapter = MemoryLakeAdapter(mock=False)
    result = adapter._filter_tool_arguments(
        "search",
        {"queries": ["hello"], "project_ids": ["proj-a"], "return_types": ["fact"],
         "top_n": 5, "unsupported_status": "approved"},
        {"properties": {"queries": {}, "project_ids": {}, "return_types": {}, "top_n": {}}},
    )
    assert result == {"queries": ["hello"], "project_ids": ["proj-a"],
                      "return_types": ["fact"], "top_n": 5}


def test_tool_argument_mapping_fails_closed_on_missing_required_field():
    adapter = MemoryLakeAdapter(mock=False)
    with pytest.raises(RuntimeError, match="not mapped"):
        adapter._filter_tool_arguments(
            "search", {"queries": ["hello"]},
            {"properties": {"queries": {}, "project_ids": {}}, "required": ["project_ids"]},
        )


@pytest.mark.parametrize("tool_name", ["remember", "forget"])
def test_live_memory_writes_and_deletes_are_blocked(tool_name):
    adapter = MemoryLakeAdapter(mock=False)
    with pytest.raises(PermissionError, match="blocks remote writes/deletes"):
        adapter._filter_tool_arguments(tool_name, {"facts": ["test"]}, {"properties": {}})


def test_live_propose_memory_never_calls_remote_remember():
    adapter = MemoryLakeAdapter(mock=False, project_id="proj-a")
    with pytest.raises(PermissionError, match="direct durable write"):
        adapter.propose_memory("test fact", project_id="proj-a")


def test_tool_argument_mapping_fails_closed_when_empty_schema_drops_arguments():
    adapter = MemoryLakeAdapter(mock=False)
    with pytest.raises(RuntimeError, match="no input properties"):
        adapter._filter_tool_arguments("search", {"queries": ["hello"]}, {"properties": {}})


def test_extract_project_ids_ignores_project_mentions_inside_fact_metadata():
    result = {
        "result": {
            "projects": [{"id": "proj-allowed", "name": "Allowed"}],
            "facts": {"items": [{"metadata": {"project_id": "proj-not-a-boundary"}}]},
            "data": {"workspace": {"boundary_projects": [{"project_id": "proj-nested-allowed"}]}},
        }
    }
    assert MemoryLakeAdapter._extract_project_ids(result) == {"proj-allowed", "proj-nested-allowed"}


def test_parser_keeps_nested_fact_as_provenance_source():
    adapter = MemoryLakeAdapter(mock=False)
    result = {"facts": [{"score": 0.91, "fact": {
        "fact_id": "fact-1", "content": "scoped content",
        "scope": "project:proj-a", "status": "verified",
    }}]}
    hits = adapter._parse_search_result(result, project_id="proj-a")
    assert len(hits) == 1
    assert hits[0].memory.raw["fact"]["scope"] == "project:proj-a"
    assert hits[0].memory.scope.project_id == "proj-a"
    assert hits[0].memory.status == MemoryStatus.VERIFIED


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


def test_inspect_result_envelope_and_project_ids_are_unwrapped():
    result = {"result": {"projects": [{"id": "proj-a"}, {"project_id": "proj-b"}]}}
    assert MemoryLakeAdapter._unwrap_result_envelope(result) == result["result"]
    assert MemoryLakeAdapter._extract_project_ids(result) == {"proj-a", "proj-b"}


def test_live_search_parser_requires_explicit_project_scope_and_preserves_unknown_status():
    adapter = MemoryLakeAdapter(mock=False, project_id="proj-a")
    raw = {"result": {"facts": [
        {"id": "fact-1", "content": "known", "scope": "project:proj-a"},
        {"id": "fact-2", "content": "unscoped"},
        {"id": "fact-3", "content": "wrong", "scope": "project:proj-b"},
    ]}}
    hits = adapter._parse_search_result(raw, project_id="proj-a")
    assert [hit.memory.memory_id for hit in hits] == ["fact-1"]
    assert hits[0].memory.status is None
    assert hits[0].memory.scope.project_id == "proj-a"


def test_search_payload_matches_actual_server_contract():
    assert MemoryLakeAdapter._search_payload("valve pipeline", "proj-a", 500) == {
        "queries": ["valve pipeline"], "project_ids": ["proj-a"],
        "return_types": ["fact"], "top_n": 100,
    }


def test_remember_and_forget_are_blocked_even_without_schema_properties():
    adapter = MemoryLakeAdapter(mock=False)
    for tool in ("remember", "forget"):
        with pytest.raises(PermissionError):
            adapter._filter_tool_arguments(tool, {"facts": ["x"]}, {})


def test_live_search_calls_inspect_before_search_and_uses_project_boundary():
    from types import SimpleNamespace
    adapter = MemoryLakeAdapter(mock=False, project_id="proj-a")
    adapter._discovered_tools = ["inspect", "search"]
    schemas = {
        "inspect": {"properties": {}},
        "search": {"properties": {"queries": {}, "project_ids": {}, "return_types": {}, "top_n": {}},
                   "required": ["queries"]},
    }
    calls = []

    class Client:
        async def call_tool(self, name, args):
            calls.append((name, args))
            if name == "inspect":
                return SimpleNamespace(is_error=False, structured_content={"result": {"projects": [{"id": "proj-a"}]}})
            return SimpleNamespace(is_error=False, structured_content={"facts": [
                {"id": "fact-a", "content": "scoped fact", "scope": "project:proj-a"}
            ]})

    async def fake_list_tools(_client):
        return ["inspect", "search"], schemas

    adapter._list_tools_async = fake_list_tools
    result = asyncio.run(adapter._search_live_async(Client(), "query", "proj-a", 4))
    assert [name for name, _ in calls] == ["inspect", "search"]
    assert calls[1][1] == {"queries": ["query"], "project_ids": ["proj-a"],
                            "return_types": ["fact"], "top_n": 4}
    assert result["facts"][0]["id"] == "fact-a"


def test_live_search_refuses_project_outside_inspect_boundary():
    from types import SimpleNamespace
    adapter = MemoryLakeAdapter(mock=False)
    adapter._discovered_tools = ["inspect", "search"]
    schemas = {"inspect": {"properties": {}}, "search": {"properties": {"queries": {}}}}

    class Client:
        async def call_tool(self, name, args):
            return SimpleNamespace(is_error=False, structured_content={"projects": [{"id": "proj-other"}]})

    async def fake_list_tools(_client):
        return ["inspect", "search"], schemas

    adapter._list_tools_async = fake_list_tools
    with pytest.raises(PermissionError, match="not present in MemoryLake inspect boundary"):
        asyncio.run(adapter._search_live_async(Client(), "query", "proj-a", 4))


def test_parser_accepts_explicit_scope_from_result_wrapper():
    adapter = MemoryLakeAdapter(mock=False)
    result = {"facts": [{"project_id": "proj-a", "fact": {
        "fact_id": "fact-2", "content": "wrapper scoped content"}}]}
    hits = adapter._parse_search_result(result, project_id="proj-a")
    assert len(hits) == 1
    assert hits[0].memory.scope.project_id == "proj-a"


def test_parser_uses_metadata_on_outer_fact_wrapper():
    adapter = MemoryLakeAdapter(mock=False)
    result = {"facts": [{
        "id": "fact-3", "metadata": {"scope": {"project_id": "proj-a"}, "status": "VERIFIED"},
        "fact": {"content": "wrapper metadata content"},
    }]}
    hits = adapter._parse_search_result(result, project_id="proj-a")
    assert len(hits) == 1
    assert hits[0].memory.status == MemoryStatus.VERIFIED
    assert hits[0].memory.content == "wrapper metadata content"


def test_live_search_calls_inspect_first_and_uses_observed_schema():
    from types import SimpleNamespace

    adapter = MemoryLakeAdapter(mock=False, project_id="proj-a")
    calls = []

    class FakeClient:
        async def list_tools(self, cursor=None):
            return SimpleNamespace(tools=[
                SimpleNamespace(name="inspect", input_schema={"properties": {}, "required": []}),
                SimpleNamespace(name="search", input_schema={
                    "properties": {
                        "queries": {}, "project_ids": {}, "return_types": {}, "top_n": {},
                    },
                    "required": ["queries"],
                }),
                SimpleNamespace(name="remember", input_schema={
                    "properties": {"facts": {}, "scope": {}}, "required": ["facts"],
                }),
                SimpleNamespace(name="forget", input_schema={
                    "properties": {"fact_ids": {}, "scope": {}}, "required": ["fact_ids"],
                }),
            ], next_cursor=None)

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            payload = ({"result": {"projects": [{"id": "proj-a"}]}}
                       if name == "inspect" else {"result": {"facts": []}})
            return SimpleNamespace(is_error=False, structured_content=payload, content=[])

    result = asyncio.run(adapter._search_live_async(FakeClient(), "agent task", "proj-a", 7))
    assert result == {"result": {"facts": []}}
    assert [name for name, _ in calls] == ["inspect", "search"]
    assert calls[1][1] == {
        "queries": ["agent task"], "project_ids": ["proj-a"],
        "return_types": ["fact"], "top_n": 7,
    }
    assert adapter.health()["mcp_discovery_verified"] is True


def test_live_search_stops_before_search_if_project_is_outside_inspect_boundary():
    from types import SimpleNamespace

    adapter = MemoryLakeAdapter(mock=False, project_id="proj-outside")
    calls = []

    class FakeClient:
        async def list_tools(self, cursor=None):
            return SimpleNamespace(tools=[
                SimpleNamespace(name="inspect", input_schema={"properties": {}, "required": []}),
                SimpleNamespace(name="search", input_schema={
                    "properties": {"queries": {}}, "required": ["queries"],
                }),
            ], next_cursor=None)

        async def call_tool(self, name, arguments):
            calls.append(name)
            return SimpleNamespace(is_error=False,
                structured_content={"projects": [{"id": "proj-allowed"}]}, content=[])

    with pytest.raises(PermissionError, match="not present in MemoryLake inspect boundary"):
        asyncio.run(adapter._search_live_async(FakeClient(), "query", "proj-outside", 5))
    assert calls == ["inspect"]
