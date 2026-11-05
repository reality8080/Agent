"""
MemoryLake MCP Adapter for Agent System v0.5 (Phase 4 — Shared Memory).

Maps the local Shared Memory contract onto the discovered MemoryLake MCP surface:
  - inspect (must precede retrieval)
  - search (project_ids, return_types, top_n)
  - remember (direct durable write; disabled until explicit approval support)
  - forget (destructive; never called automatically)

The server tool names and schemas are authoritative. Local proposals stay in
Agent System's SQLite Event Ledger; they are not silently sent to remember.

Design constraints (Ponytail + Spec):
  - Reuse MCP SDK / HTTP client only; no extra agent framework.
  - Do NOT replace LangGraph checkpointer or SQLite Event Ledger.
  - OAuth 2.1 is handled by the official MCP SDK; Personal REST API Keys
    are not sent to the OAuth-protected MCP endpoint.
  - Never embed secrets in source.
  - Never auto-promote proposed → approved/verified.
  - Conflicts are surfaced, never silently overwritten.
  - Mock only when MEMORYLAKE_MOCK=1 (or mock=True); no auto-mock on missing token.

Discovery note:
  Exact tool parameter schemas are re-read from the authenticated endpoint.
  The observed endpoint contract uses inspect/search/remember/forget; the
  adapter fails closed when a required field or scope cannot be verified.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared Memory Contract (Spec §8)
# ---------------------------------------------------------------------------


class MemoryType(str, Enum):
    BACKGROUND = "background"
    FACT = "fact"
    EVENT = "event"
    DIALOGUE = "dialogue"
    REFLECTION = "reflection"
    SKILL = "skill"


class MemoryStatus(str, Enum):
    PROPOSED = "proposed"
    VERIFIED = "verified"
    DISPUTED = "disputed"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


@dataclass
class MemoryScope:
    user_id: Optional[str] = None
    project_id: Optional[str] = None
    task_id: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class SharedMemory:
    """Canonical in-process representation of Spec Shared Memory Contract."""

    memory_id: str
    memory_type: MemoryType
    content: str
    scope: MemoryScope
    status: Optional[MemoryStatus] = MemoryStatus.PROPOSED
    source_ref: Optional[str] = None
    evidence_ref: List[str] = field(default_factory=list)
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    confidence: float = 0.0
    version: int = 1
    supersedes: Optional[str] = None
    conflicts_with: List[str] = field(default_factory=list)
    expires_at: Optional[str] = None
    # Remote identifiers returned by MemoryLake (opaque to Spec contract)
    remote_id: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_contract_dict(self) -> Dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "memory_type": self.memory_type.value,
            "scope": self.scope.as_dict(),
            "content": self.content,
            "status": self.status.value if self.status else None,
            "source_ref": self.source_ref,
            "evidence_ref": list(self.evidence_ref),
            "created_by": self.created_by,
            "created_at": self.created_at,
            "confidence": self.confidence,
            "version": self.version,
            "supersedes": self.supersedes,
            "conflicts_with": list(self.conflicts_with),
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_contract_dict(cls, data: Dict[str, Any]) -> "SharedMemory":
        scope_data = data.get("scope") or {}
        return cls(
            memory_id=data.get("memory_id") or str(uuid.uuid4()),
            memory_type=MemoryType(data.get("memory_type", MemoryType.FACT.value)),
            content=data.get("content") or "",
            scope=MemoryScope(
                user_id=scope_data.get("user_id"),
                project_id=scope_data.get("project_id"),
                task_id=scope_data.get("task_id"),
            ),
            status=(MemoryStatus(data["status"]) if data.get("status") in {item.value for item in MemoryStatus} else None),
            source_ref=data.get("source_ref"),
            evidence_ref=list(data.get("evidence_ref") or []),
            created_by=data.get("created_by"),
            created_at=data.get("created_at"),
            confidence=float(data.get("confidence") or 0.0),
            version=int(data.get("version") or 1),
            supersedes=data.get("supersedes"),
            conflicts_with=list(data.get("conflicts_with") or []),
            expires_at=data.get("expires_at"),
            remote_id=data.get("remote_id"),
            raw=dict(data.get("raw") or {}),
        )


@dataclass
class MemorySearchHit:
    memory: SharedMemory
    score: Optional[float] = None
    snippet: Optional[str] = None


@dataclass
class ConflictReport:
    conflicts: List[Dict[str, Any]]
    query: Optional[str] = None
    mock: bool = False
    available: bool = True
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class MemoryLakeKeyringStorage:
    """Persist MCP OAuth tokens and client registration in the OS credential vault."""

    SERVICE_NAME = "AgentSystem.MemoryLake.OAuth"

    def __init__(self, server_url: str, redirect_uri: str = ""):
        try:
            import keyring
        except ImportError as exc:
            raise RuntimeError(
                "MemoryLake OAuth requires the 'keyring' package; install project adapter dependencies."
            ) from exc

        backend = keyring.get_keyring()
        if float(getattr(backend, "priority", 0) or 0) <= 0:
            raise RuntimeError(
                "No secure OS credential backend is available; refusing plaintext OAuth token storage."
            )

        self._keyring = keyring
        self.backend_name = type(backend).__name__
        namespace = f"{server_url}|{redirect_uri}"
        self._username = hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:32]

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        metadata_raw = self._keyring.get_password(
            self.SERVICE_NAME, f"{self._username}:token_metadata"
        )
        access_token = self._keyring.get_password(
            self.SERVICE_NAME, f"{self._username}:access_token"
        )
        if not metadata_raw or not access_token:
            return None
        metadata = json.loads(metadata_raw)
        refresh_token = self._keyring.get_password(
            self.SERVICE_NAME, f"{self._username}:refresh_token"
        )
        return OAuthToken.model_validate({
            **metadata, "access_token": access_token, "refresh_token": refresh_token
        })

    async def set_tokens(self, tokens) -> None:
        # Keep access and refresh tokens in separate vault entries to avoid
        # Windows Credential Manager's per-credential blob size limit.
        metadata = tokens.model_dump(
            mode="json", exclude_none=True, exclude={"access_token", "refresh_token"}
        )
        self._keyring.set_password(
            self.SERVICE_NAME, f"{self._username}:access_token", tokens.access_token
        )
        if tokens.refresh_token:
            self._keyring.set_password(
                self.SERVICE_NAME, f"{self._username}:refresh_token", tokens.refresh_token
            )
        # OAuth token refresh responses may omit refresh_token; in that case
        # the previous refresh token remains valid and must not be deleted.
        self._keyring.set_password(
            self.SERVICE_NAME, f"{self._username}:token_metadata", json.dumps(metadata)
        )

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._keyring.get_password(self.SERVICE_NAME, f"{self._username}:client_info")
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def set_client_info(self, client_info) -> None:
        payload = client_info.model_dump_json(exclude_none=True)
        self._keyring.set_password(self.SERVICE_NAME, f"{self._username}:client_info", payload)


class _LoopbackOAuthCallback:
    """Short-lived localhost callback listener for native OAuth authorization-code flow."""

    def __init__(self, timeout: float = 300.0):
        self.host = "127.0.0.1"
        self.port = int(os.getenv("MEMORYLAKE_OAUTH_CALLBACK_PORT", "8765"))
        if not 1 <= self.port <= 65535:
            raise ValueError("MEMORYLAKE_OAUTH_CALLBACK_PORT must be between 1 and 65535")
        self.path = "/oauth/callback"
        self.redirect_uri = f"http://{self.host}:{self.port}{self.path}"
        self.timeout = timeout
        self._server = None
        self._result = None

    async def start(self) -> None:
        self._result = asyncio.get_running_loop().create_future()
        try:
            self._server = await asyncio.start_server(self._handle, self.host, self.port)
        except OSError as exc:
            raise RuntimeError(
                f"OAuth callback port {self.port} is unavailable. Set MEMORYLAKE_OAUTH_CALLBACK_PORT to another free localhost port."
            ) from exc

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def open_browser(self, authorization_url: str) -> None:
        if not webbrowser.open(authorization_url, new=1, autoraise=True):
            raise RuntimeError(
                "Could not open the default browser for MemoryLake OAuth. Configure a default browser and retry."
            )

    async def wait_for_callback(self):
        from mcp.client.auth import AuthorizationCodeResult

        if self._result is None:
            raise RuntimeError("OAuth callback listener was not started")
        result = await asyncio.wait_for(asyncio.shield(self._result), timeout=self.timeout)
        if not isinstance(result, AuthorizationCodeResult):
            raise RuntimeError("MemoryLake OAuth callback returned an invalid result")
        return result

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        status = "400 Bad Request"
        body = "Authorization response was not accepted. Return to Agent System."
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            parts = request_line.decode("ascii", errors="replace").strip().split()
            if len(parts) < 2 or parts[0] != "GET":
                raise ValueError("Invalid OAuth callback request")

            # Consume headers; the callback only accepts a small GET request.
            header_bytes = 0
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                header_bytes += len(line)
                if header_bytes > 16384:
                    raise ValueError("OAuth callback headers are too large")
                if line in (b"\r\n", b"\n", b""):
                    break

            parsed = urlsplit(parts[1])
            if parsed.path != self.path:
                status = "404 Not Found"
                body = "Unknown callback path. You may close this page."
            else:
                params = parse_qs(parsed.query, keep_blank_values=True)
                if params.get("error"):
                    error = params["error"][0]
                    description = params.get("error_description", [""])[0]
                    if self._result is not None and not self._result.done():
                        self._result.set_exception(
                            RuntimeError(f"MemoryLake OAuth authorization was denied: {error} {description}".strip())
                        )
                else:
                    code = params.get("code", [None])[0]
                    state = params.get("state", [None])[0]
                    issuer = params.get("iss", [None])[0]
                    if not code:
                        raise ValueError("OAuth callback did not contain an authorization code")
                    from mcp.client.auth import AuthorizationCodeResult

                    if self._result is not None and not self._result.done():
                        # Pass state and issuer unchanged; the SDK validates both.
                        self._result.set_result(
                            AuthorizationCodeResult(code=code, state=state, iss=issuer)
                        )
                    status = "200 OK"
                    body = "MemoryLake authorization response received. You can return to Agent System."
        except Exception as exc:
            if self._result is not None and not self._result.done():
                self._result.set_exception(exc)
        finally:
            encoded = body.encode("utf-8")
            writer.write(
                f"HTTP/1.1 {status}\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                "Cache-Control: no-store\r\n"
                "Connection: close\r\n"
                f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii")
                + encoded
            )
            try:
                await writer.drain()
            except Exception:
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


class MemoryLakeAdapter:
    """MemoryLake MCP adapter using OAuth 2.1 and Streamable HTTP."""

    DEFAULT_URL = "https://app.memorylake.ai/memorylake/mcp/v2"
    TOOL_INSPECT = "inspect"
    TOOL_SEARCH = "search"
    TOOL_REMEMBER = "remember"
    TOOL_FORGET = "forget"

    def __init__(
        self,
        mcp_url: Optional[str] = None,
        token: Optional[str] = None,
        project_id: Optional[str] = None,
        user_id: Optional[str] = None,
        mock: Optional[bool] = None,
        timeout: float = 30.0,
    ):
        self.mcp_url = (mcp_url or os.getenv("MEMORYLAKE_MCP_URL") or self.DEFAULT_URL).rstrip("/")
        # Backward-compatible parameter only. A Personal REST API Key is NOT an MCP OAuth token.
        legacy_token = token if token is not None else os.getenv("MEMORYLAKE_MCP_TOKEN")
        self._legacy_token_configured = bool(legacy_token)
        if self._legacy_token_configured:
            logger.warning(
                "MEMORYLAKE_MCP_TOKEN is ignored for this OAuth-protected MCP endpoint; use browser authorization."
            )
        self.default_project_id = project_id or os.getenv("MEMORYLAKE_PROJECT_ID")
        self.default_user_id = user_id or os.getenv("MEMORYLAKE_USER_ID")
        self.timeout = float(timeout)

        if mock is None:
            mock = os.getenv("MEMORYLAKE_MOCK", "").lower() in ("1", "true", "yes")
        self.mock = bool(mock)

        self._discovered_tools: Optional[List[str]] = None
        self._tool_schemas: Dict[str, Dict[str, Any]] = {}
        self._mock_store: Dict[str, SharedMemory] = {}
        self._oauth_authenticated = False
        self._mcp_discovery_verified = False
        self._inspect_verified = False
        self._read_verified = False
        self._write_verified = False  # no remote write is enabled in this integration
        self._last_search_diagnostics: Dict[str, Any] = {}
        self._last_error_type: Optional[str] = None
        self._credential_backend: Optional[str] = None

        if self.mock:
            logger.info("MemoryLakeAdapter running in MOCK mode (MEMORYLAKE_MOCK=1)")
        else:
            logger.info("MemoryLakeAdapter configured for OAuth MCP at %s", self.mcp_url)

    # ------------------------------------------------------------------
    # Public high-level API (Spec-aligned)
    # ------------------------------------------------------------------

    def propose_memory(
        self,
        content: str,
        memory_type: MemoryType | str = MemoryType.FACT,
        *,
        project_id: Optional[str] = None,
        task_id: Optional[str] = None,
        user_id: Optional[str] = None,
        source_ref: Optional[str] = None,
        evidence_ref: Optional[Sequence[str]] = None,
        created_by: Optional[str] = None,
        confidence: float = 0.0,
        status: MemoryStatus | str = MemoryStatus.PROPOSED,
    ) -> SharedMemory:
        if isinstance(memory_type, str):
            memory_type = MemoryType(memory_type)
        if isinstance(status, str):
            status = MemoryStatus(status)
        if status in (MemoryStatus.APPROVED, MemoryStatus.VERIFIED) and (
            created_by is None or not str(created_by).startswith("user")
        ):
            logger.warning("Refusing agent self-promotion to %s; forcing proposed", status.value)
            status = MemoryStatus.PROPOSED

        scope = MemoryScope(
            user_id=user_id or self.default_user_id,
            project_id=project_id or self.default_project_id,
            task_id=task_id,
        )
        if not scope.project_id:
            raise ValueError("project_id is required for MemoryLake writes (project scope)")

        mem = SharedMemory(
            memory_id=str(uuid.uuid4()), memory_type=memory_type, content=content,
            scope=scope, status=status, source_ref=source_ref,
            evidence_ref=list(evidence_ref or []), created_by=created_by or "agent",
            created_at=datetime.now(timezone.utc).isoformat(), confidence=float(confidence), version=1,
        )
        if self.mock:
            self._mock_store[mem.memory_id] = mem
            mem.remote_id = f"mock-{mem.memory_id}"
            return mem

        raise PermissionError(
            "Live MemoryLake writes are disabled: the discovered 'remember' tool is a direct durable write, "
            "not a pending proposal API. Store proposals locally and require explicit user approval first."
        )

    def inspect(self) -> Dict[str, Any]:
        """Read the MemoryLake boundary/context without changing remote data."""
        if self.mock:
            self._inspect_verified = True
            return {"projects": ([{"id": self.default_project_id}] if self.default_project_id else []),
                    "facts": {"items": []}}
        result = self._run_live(lambda client: self._inspect_live_async(client))
        self._inspect_verified = True
        return self._unwrap_result_envelope(result)

    def search(
        self, query: str, *, project_id: Optional[str] = None,
        memory_type: Optional[MemoryType | str] = None,
        status: Optional[MemoryStatus | str] = None, top_k: int = 8,
    ) -> List[MemorySearchHit]:
        project_id = project_id or self.default_project_id
        if not project_id:
            raise ValueError("project_id is required for project-scoped MemoryLake retrieval")
        if isinstance(memory_type, str):
            memory_type = MemoryType(memory_type)
        if isinstance(status, str):
            status = MemoryStatus(status)
        if self.mock:
            return self._mock_search(query, project_id, memory_type, status, top_k)
        if memory_type not in (None, MemoryType.FACT):
            return []  # The live MemoryLake search contract currently returns facts only.

        result = self._run_live(
            lambda client: self._search_live_async(client, query, project_id, top_k)
        )
        if not self._has_search_result_shape(result):
            raise RuntimeError("MemoryLake search returned an unrecognized response shape; retrieval is not verified.")
        candidates = self._search_items(result)
        hits = self._parse_search_result(result, project_id=project_id)
        self._last_search_diagnostics = {
            "response_keys": sorted(self._unwrap_result_envelope(result).keys())
                if isinstance(self._unwrap_result_envelope(result), dict) else [],
            "candidate_count": len(candidates),
            "accepted_project_scoped_count": len(hits),
            "rejected_or_unparsed_count": max(0, len(candidates) - len(hits)),
        }
        self._read_verified = True
        if status is not None:
            hits = [hit for hit in hits if hit.memory.status == status]
        return hits

    def detect_conflicts(
        self, query: Optional[str] = None, *, project_id: Optional[str] = None,
    ) -> ConflictReport:
        """Report availability honestly; the current live toolset has no conflict API."""
        if self.mock:
            return ConflictReport(
                conflicts=[], query=query, mock=True, available=False,
                reason="Conflict detection is not implemented by the current adapter; no conflict check was performed.",
            )
        return ConflictReport(
            conflicts=[], query=query, mock=False, available=False,
            reason="The discovered MemoryLake toolset does not expose detect_conflicts; no conflict check was performed.",
        )

    def list_resources(self) -> List[Dict[str, Any]]:
        if self.mock:
            return [
                {"uri": f"memorylake://mock/{m.memory_id}", "name": m.memory_id,
                 "description": m.content[:120], "mimeType": "application/json"}
                for m in self._mock_store.values()
            ]
        return self._run_live(lambda client: self._list_resources_async(client))

    def read_resource(self, uri: str) -> Dict[str, Any]:
        if self.mock:
            mid = uri.rstrip("/").split("/")[-1]
            mem = self._mock_store.get(mid)
            if not mem:
                raise KeyError(f"mock resource not found: {uri}")
            return {"uri": uri, "contents": [mem.to_contract_dict()]}
        return self._run_live(lambda client: self._read_resource_async(client, uri))

    def promote(
        self, memory_id: str, new_status: MemoryStatus | str, *, actor: str,
        reason: Optional[str] = None,
    ) -> SharedMemory:
        if isinstance(new_status, str):
            new_status = MemoryStatus(new_status)
        if not str(actor).startswith(("user", "owner", "orchestrator")):
            raise PermissionError("Only user/owner/orchestrator may promote memory status")
        if self.mock:
            mem = self._mock_store.get(memory_id)
            if not mem:
                raise KeyError(memory_id)
            mem.status = new_status
            mem.version += 1
            if reason:
                mem.raw["promotion_reason"] = reason
            return mem
        raise NotImplementedError(
            "Live promotion is unavailable until MemoryLake exposes and documents an update/promote tool."
        )

    # ------------------------------------------------------------------
    # Discovery & health
    # ------------------------------------------------------------------

    @property
    def discovered_tool_schemas(self) -> Dict[str, Dict[str, Any]]:
        return {name: dict(schema) for name, schema in self._tool_schemas.items()}

    def discover_tools(self) -> List[str]:
        if self.mock:
            self._discovered_tools = [
                "inspect", "search", "list_files", "inspect_file", "run_code", "remember", "forget"
            ]
            self._tool_schemas = {}
            return list(self._discovered_tools)

        tools, schemas = self._run_live(self._list_tools_async)
        self._cache_tool_discovery(tools, schemas)
        self._mcp_discovery_verified = True
        return list(self._discovered_tools or [])

    def health(self) -> Dict[str, Any]:
        if self.mock:
            status = "mock"
        elif self._read_verified:
            status = "read_verified"
        elif self._inspect_verified:
            status = "inspect_verified"
        elif self._mcp_discovery_verified:
            status = "mcp_discovery_verified"
        elif self._oauth_authenticated:
            status = "oauth_authenticated_unverified"
        else:
            status = "oauth_required_or_unverified"
        return {
            "mock": self.mock,
            "mcp_url": self.mcp_url,
            "has_legacy_token": self._legacy_token_configured,
            "oauth_authenticated": self._oauth_authenticated,
            "credential_backend": self._credential_backend,
            "default_project_id": self.default_project_id,
            "discovered_tools": list(self._discovered_tools or []),
            "mcp_discovery_verified": self._mcp_discovery_verified,
            "inspect_verified": self._inspect_verified,
            "read_verified": self._read_verified,
            "write_verified": self._write_verified,
            "ready_for_retrieval": self._read_verified,
            "ready_for_writes": False,
            "last_error_type": self._last_error_type,
            "status": status,
            # Backward-compatible field, deliberately stricter than discovery alone.
            "live_ready": self._read_verified,
        }

    # ------------------------------------------------------------------
    # Payload mapping, MCP transport and result parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _search_payload(query: str, project_id: str, top_k: int) -> Dict[str, Any]:
        return {
            "queries": [str(query or "project context")],
            "project_ids": [project_id],
            "return_types": ["fact"],
            "top_n": max(1, min(int(top_k), 100)),
        }

    @staticmethod
    def _run_awaitable(awaitable):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(awaitable)
        # A sync adapter may be called from an async host. Run its private loop in a worker.
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="memorylake-mcp") as pool:
            return pool.submit(asyncio.run, awaitable).result()

    def _run_live(self, operation: Callable[[Any], Awaitable[Any]]):
        try:
            result = self._run_awaitable(self._run_live_async(operation))
            self._last_error_type = None
            return result
        except Exception as exc:
            self._last_error_type = type(exc).__name__
            raise

    async def _run_live_async(self, operation: Callable[[Any], Awaitable[Any]]):
        try:
            import httpx2
            from pydantic import AnyUrl
            from mcp import Client
            from mcp.client.auth import OAuthClientProvider
            from mcp.client.streamable_http import streamable_http_client
            from mcp.shared.auth import OAuthClientMetadata
        except ImportError as exc:
            raise RuntimeError(
                "MemoryLake OAuth requires MCP SDK v2.3+ and its httpx2 transport dependencies."
            ) from exc

        callback = _LoopbackOAuthCallback(timeout=max(300.0, self.timeout))
        await callback.start()
        try:
            storage = MemoryLakeKeyringStorage(self.mcp_url, callback.redirect_uri)
            self._credential_backend = storage.backend_name
            metadata = OAuthClientMetadata(
                client_name="Agent System",
                application_type="native",
                redirect_uris=[AnyUrl(callback.redirect_uri)],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
            )
            oauth = OAuthClientProvider(
                server_url=self.mcp_url,
                client_metadata=metadata,
                storage=storage,
                redirect_handler=callback.open_browser,
                callback_handler=callback.wait_for_callback,
            )
            timeout = httpx2.Timeout(self.timeout, read=max(300.0, self.timeout))
            async with httpx2.AsyncClient(auth=oauth, timeout=timeout) as http_client:
                transport = streamable_http_client(self.mcp_url, http_client=http_client)
                async with Client(transport, read_timeout_seconds=self.timeout) as client:
                    self._oauth_authenticated = True
                    return await operation(client)
        finally:
            await callback.close()

    @staticmethod
    async def _list_tools_async(client) -> tuple[List[str], Dict[str, Dict[str, Any]]]:
        names: List[str] = []
        schemas: Dict[str, Dict[str, Any]] = {}
        cursor = None
        while True:
            page = await client.list_tools(cursor=cursor)
            for tool in page.tools:
                name = str(tool.name)
                names.append(name)
                schema = getattr(tool, "input_schema", None) or {}
                if hasattr(schema, "model_dump"):
                    schema = schema.model_dump(mode="json", by_alias=True)
                schema_data = dict(schema) if isinstance(schema, dict) else {}
                description = getattr(tool, "description", None)
                if description:
                    schema_data["_tool_description"] = str(description)
                title = getattr(tool, "title", None)
                if title:
                    schema_data["_tool_title"] = str(title)
                schemas[name] = schema_data
            cursor = getattr(page, "next_cursor", None)
            if not cursor:
                break
        return names, schemas

    def _cache_tool_discovery(self, tools: List[str], schemas: Dict[str, Dict[str, Any]]) -> None:
        self._discovered_tools = list(tools)
        self._tool_schemas = dict(schemas)
        self._mcp_discovery_verified = True

    def _filter_tool_arguments(self, name: str, arguments: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
        if name in {self.TOOL_REMEMBER, self.TOOL_FORGET}:
            action = "write durable facts" if name == self.TOOL_REMEMBER else "delete facts"
            raise PermissionError(f"MemoryLake '{name}' would {action}; this integration blocks remote writes/deletes.")
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            if arguments or schema.get("required"):
                raise RuntimeError(f"MemoryLake tool '{name}' schema is missing its properties mapping")
            return {}
        if arguments and not properties:
            raise RuntimeError(f"MemoryLake tool '{name}' exposes no input properties; refusing to drop supplied arguments")
        filtered = {key: value for key, value in arguments.items() if key in properties}
        required = schema.get("required") or []
        missing = [key for key in required if key not in filtered]
        if missing:
            raise RuntimeError(
                f"MemoryLake tool '{name}' requires arguments not mapped by this adapter: {missing}. "
                "Inspect discovered_tool_schemas before changing the payload mapping."
            )
        return filtered

    @staticmethod
    def _tool_result_value(result: Any, tool_name: str) -> Any:
        if bool(getattr(result, "is_error", False)):
            messages = [
                str(getattr(block, "text", ""))
                for block in (getattr(result, "content", None) or [])
                if getattr(block, "type", None) == "text"
            ]
            detail = "; ".join(x for x in messages if x)[:500]
            raise RuntimeError(f"MemoryLake tool '{tool_name}' returned an error: {detail or 'unspecified tool error'}")

        structured = getattr(result, "structured_content", None)
        if structured is not None:
            return structured

        values: List[Any] = []
        for block in getattr(result, "content", None) or []:
            if getattr(block, "type", None) == "text":
                text = str(getattr(block, "text", ""))
                try:
                    values.append(json.loads(text))
                except json.JSONDecodeError:
                    values.append(text)
        if len(values) == 1:
            return values[0]
        return values

    async def _call_discovered_tool_async(
        self, client, name: str, arguments: Dict[str, Any], schemas: Dict[str, Dict[str, Any]],
    ) -> Any:
        tools = self._discovered_tools or []
        if name not in tools:
            raise RuntimeError(
                f"MemoryLake MCP server does not expose required tool '{name}'. Discovered tools: {tools}"
            )
        filtered = self._filter_tool_arguments(name, arguments, schemas.get(name, {}))
        result = await client.call_tool(name, filtered)
        return self._tool_result_value(result, name)

    async def _call_tool_async(self, client, name: str, arguments: Dict[str, Any]):
        tools, schemas = await self._list_tools_async(client)
        self._cache_tool_discovery(tools, schemas)
        return await self._call_discovered_tool_async(client, name, arguments, schemas)

    async def _inspect_live_async(self, client) -> Any:
        tools, schemas = await self._list_tools_async(client)
        self._cache_tool_discovery(tools, schemas)
        result = await self._call_discovered_tool_async(client, self.TOOL_INSPECT, {}, schemas)
        return self._unwrap_result_envelope(result)

    async def _search_live_async(self, client, query: str, project_id: str, top_k: int) -> Any:
        # The MemoryLake tool description requires inspect before search. Do both
        # in the same MCP session and fail closed if the configured project is
        # not among the current authorization boundary projects.
        tools, schemas = await self._list_tools_async(client)
        self._cache_tool_discovery(tools, schemas)
        inspect_result = await self._call_discovered_tool_async(client, self.TOOL_INSPECT, {}, schemas)
        self._inspect_verified = True
        allowed_projects = self._extract_project_ids(self._unwrap_result_envelope(inspect_result))
        if project_id not in allowed_projects:
            raise PermissionError(
                "Configured MEMORYLAKE_PROJECT_ID is not present in MemoryLake inspect boundary; refusing retrieval."
            )
        search_args = self._search_payload(query, project_id, top_k)
        return await self._call_discovered_tool_async(client, self.TOOL_SEARCH, search_args, schemas)

    def _call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        if self.mock:
            raise RuntimeError("_call_tool must not be used in MOCK mode")
        return self._run_live(lambda client: self._call_tool_async(client, name, arguments))

    async def _list_resources_async(self, client) -> List[Dict[str, Any]]:
        result = await client.list_resources()
        return [
            item.model_dump(mode="json", by_alias=True) if hasattr(item, "model_dump") else dict(item)
            for item in result.resources
        ]

    async def _read_resource_async(self, client, uri: str) -> Dict[str, Any]:
        result = await client.read_resource(uri)
        return result.model_dump(mode="json", by_alias=True) if hasattr(result, "model_dump") else dict(result)

    @staticmethod
    def _unwrap_result_envelope(result: Any) -> Any:
        """Unwrap MCP/server result envelopes without assuming a fixed depth."""
        for _ in range(4):
            if isinstance(result, dict) and set(result) == {"result"}:
                result = result["result"]
                continue
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                    continue
                except json.JSONDecodeError:
                    return result
            break
        return result

    @classmethod
    def _extract_project_ids(cls, result: Any) -> set[str]:
        """Extract IDs only from explicit project lists in inspect/boundary containers."""
        result = cls._unwrap_result_envelope(result)
        found: set[str] = set()
        containers = [result]
        # Inspect responses may wrap the boundary under one of these known
        # structural keys. Do not recurse through facts/files: their metadata
        # must not accidentally expand the authorization boundary.
        container_keys = ("data", "boundary", "workspace", "context", "inspection")
        cursor = 0
        while cursor < len(containers):
            parent = containers[cursor]
            cursor += 1
            if isinstance(parent, dict):
                for key in container_keys:
                    child = parent.get(key)
                    if isinstance(child, dict) and all(child is not seen for seen in containers):
                        containers.append(child)
        for container in containers:
            if not isinstance(container, dict):
                continue
            for key in ("projects", "boundary_projects"):
                entries = container.get(key)
                if not isinstance(entries, list):
                    continue
                for item in entries:
                    if isinstance(item, str):
                        found.add(item)
                    elif isinstance(item, dict):
                        project_id = item.get("project_id") or item.get("id")
                        if isinstance(project_id, str) and project_id:
                            found.add(project_id)
        return found

    @staticmethod
    def _explicit_project_id(item: Dict[str, Any]) -> Optional[str]:
        """Read an explicit project scope; never infer it from the query argument."""
        candidates = [item]
        for key in ("metadata", "meta", "attributes", "fact"):
            nested = item.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)
        for candidate in candidates:
            scope = candidate.get("scope")
            if isinstance(scope, str) and scope.startswith("project:"):
                return scope.split(":", 1)[1] or None
            if isinstance(scope, dict):
                value = scope.get("project_id") or scope.get("projectId")
                if isinstance(value, str) and value:
                    return value
            value = candidate.get("project_id") or candidate.get("projectId")
            if isinstance(value, str) and value:
                return value
            project = candidate.get("project")
            if isinstance(project, dict):
                value = project.get("id") or project.get("project_id")
                if isinstance(value, str) and value:
                    return value
        return None

    @classmethod
    def _has_search_result_shape(cls, result: Any) -> bool:
        result = cls._unwrap_result_envelope(result)
        if isinstance(result, list):
            return True
        if not isinstance(result, dict):
            return False
        for key in ("facts", "memories", "results", "items", "matches", "data"):
            if key in result:
                value = result[key]
                return isinstance(value, (list, dict))
        return any(key in result for key in ("fact_id", "memory_id", "content", "text"))

    @classmethod
    def _search_items(cls, result: Any) -> List[Any]:
        result = cls._unwrap_result_envelope(result)
        if isinstance(result, list):
            return result
        if not isinstance(result, dict):
            return []
        for key in ("facts", "memories", "results", "items", "matches", "data"):
            value = result.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                for nested_key in ("items", "results", "matches"):
                    nested = value.get(nested_key)
                    if isinstance(nested, list):
                        return nested
                if any(field in value for field in ("fact_id", "memory_id", "content", "text")):
                    return [value]
        # Some tools return a single fact object rather than a result list.
        if any(key in result for key in ("fact_id", "memory_id", "content", "text")):
            return [result]
        return []

    def _parse_search_result(self, result: Any, project_id: Optional[str] = None) -> List[MemorySearchHit]:
        hits: List[MemorySearchHit] = []
        for item in self._search_items(result):
            if not isinstance(item, dict):
                continue
            nested_fact = item.get("fact")
            raw = nested_fact if isinstance(nested_fact, dict) else item
            raw_meta = next((raw.get(key) for key in ("metadata", "meta", "attributes")
                             if isinstance(raw.get(key), dict)), {})
            outer_meta = next((item.get(key) for key in ("metadata", "meta", "attributes")
                               if isinstance(item.get(key), dict)), {})
            meta = raw_meta or outer_meta or raw
            content = (raw.get("content") or raw.get("text") or raw.get("value") or raw.get("fact")
                       or meta.get("content") or meta.get("text") or item.get("content") or item.get("text"))
            if not isinstance(content, str) or not content.strip():
                continue
            explicit_project_id = self._explicit_project_id(raw) or self._explicit_project_id(item)
            if explicit_project_id is None or (project_id and explicit_project_id != project_id):
                # Search is project-filtered remotely, but we still require
                # explicit provenance in the returned fact before using it.
                continue
            mid = (raw.get("fact_id") or raw.get("memory_id") or raw.get("id")
                   or meta.get("fact_id") or meta.get("memory_id") or meta.get("id")
                   or item.get("fact_id") or item.get("memory_id") or item.get("id"))
            if not isinstance(mid, str) or not mid:
                continue
            try:
                mtype = MemoryType(meta.get("memory_type") or meta.get("type") or item.get("type") or "fact")
            except ValueError:
                mtype = MemoryType.FACT
            raw_status = meta.get("status") or raw.get("status") or item.get("status")
            try:
                normalized_status = str(raw_status).strip().lower() if raw_status is not None else None
                status = MemoryStatus(normalized_status) if normalized_status in {value.value for value in MemoryStatus} else None
            except (TypeError, ValueError):
                status = None
            scope_data = raw.get("scope") if isinstance(raw.get("scope"), dict) else {}
            if not scope_data and isinstance(meta.get("scope"), dict):
                scope_data = meta["scope"]
            if not scope_data and isinstance(item.get("scope"), dict):
                scope_data = item["scope"]
            mem = SharedMemory(
                memory_id=str(mid), memory_type=mtype, content=content,
                scope=MemoryScope(
                    user_id=scope_data.get("user_id") or meta.get("user_id") or item.get("user_id"),
                    project_id=explicit_project_id,
                    task_id=scope_data.get("task_id") or meta.get("task_id") or item.get("task_id"),
                ),
                status=status, source_ref=meta.get("source_ref") or item.get("source_ref"),
                evidence_ref=list(meta.get("evidence_ref") or item.get("evidence_ref") or []),
                created_by=meta.get("created_by") or item.get("created_by"),
                created_at=meta.get("created_at") or item.get("created_at"),
                confidence=float(meta.get("confidence") or item.get("confidence") or 0.0),
                version=int(meta.get("version") or item.get("version") or 1),
                remote_id=str(mid), raw=item,
            )
            score = item.get("score") or raw.get("score") or item.get("similarity") or raw.get("similarity")
            snippet = item.get("snippet") or raw.get("snippet") or content[:200]
            hits.append(MemorySearchHit(memory=mem, score=score, snippet=snippet))
        return hits

    def _mock_search(
        self, query: str, project_id: Optional[str], memory_type: Optional[MemoryType],
        status: Optional[MemoryStatus], top_k: int,
    ) -> List[MemorySearchHit]:
        q = (query or "").lower()
        hits: List[MemorySearchHit] = []
        for mem in self._mock_store.values():
            if project_id and mem.scope.project_id and mem.scope.project_id != project_id:
                continue
            if memory_type and mem.memory_type != memory_type:
                continue
            if status and mem.status != status:
                continue
            if q and q not in mem.content.lower() and q not in (mem.source_ref or "").lower():
                if not any(tok in mem.content.lower() for tok in q.split() if len(tok) > 2):
                    continue
            hits.append(MemorySearchHit(memory=mem, score=0.9, snippet=mem.content[:200]))
        return hits[:top_k]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_memorylake_adapter(
    mock: Optional[bool] = None,
    project_id: Optional[str] = None,
) -> MemoryLakeAdapter:
    """Factory used by tests and orchestrator wiring."""
    return MemoryLakeAdapter(mock=mock, project_id=project_id)


__all__ = [
    "MemoryType",
    "MemoryStatus",
    "MemoryScope",
    "SharedMemory",
    "MemorySearchHit",
    "ConflictReport",
    "MemoryLakeAdapter",
    "create_memorylake_adapter",
]