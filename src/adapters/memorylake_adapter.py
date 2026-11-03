"""
MemoryLake MCP Adapter for Agent System v0.5 (Phase 4 — Shared Memory).

Maps Spec Shared Memory Contract onto the public MemoryLake MCP surface:
  - resources/list, resources/read
  - tools/call: search_memory
  - tools/call: add_memory
  - tools/call: detect_conflicts

Design constraints (Ponytail + Spec):
  - Reuse MCP SDK / HTTP client only; no extra agent framework.
  - Do NOT replace LangGraph checkpointer or SQLite Event Ledger.
  - Token optional: MCP clients (VS Code / Cursor) use OAuth consent;
    programmatic callers may still pass MEMORYLAKE_MCP_TOKEN.
  - Never embed secrets in source.
  - Never auto-promote proposed → approved/verified.
  - Conflicts are surfaced, never silently overwritten.
  - Mock only when MEMORYLAKE_MOCK=1 (or mock=True); no auto-mock on missing token.

Discovery note:
  Exact tool parameter schemas must be re-validated at first successful
  session (list_tools). This adapter uses the documented method names
  and a conservative JSON payload shape that matches the public docs.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

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
    status: MemoryStatus = MemoryStatus.PROPOSED
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
            "status": self.status.value,
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
            status=MemoryStatus(data.get("status", MemoryStatus.PROPOSED.value)),
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


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class MemoryLakeAdapter:
    """
    Thin client over MemoryLake MCP (HTTP; Bearer token optional).

    Environment:
      MEMORYLAKE_MCP_URL      default https://mcp.memorylake.ai
      MEMORYLAKE_MCP_TOKEN    optional Bearer token (programmatic use)
      MEMORYLAKE_PROJECT_ID   optional default project_id for scope
      MEMORYLAKE_USER_ID      optional default user_id for scope
      MEMORYLAKE_MOCK         force mock (1/true); default is live
    """

    DEFAULT_URL = "https://mcp.memorylake.ai"

    # Documented MCP tool names (must be re-discovered on first live session)
    TOOL_SEARCH = "search_memory"
    TOOL_ADD = "add_memory"
    TOOL_DETECT_CONFLICTS = "detect_conflicts"

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
        self.token = token if token is not None else os.getenv("MEMORYLAKE_MCP_TOKEN")
        self.default_project_id = project_id or os.getenv("MEMORYLAKE_PROJECT_ID")
        self.default_user_id = user_id or os.getenv("MEMORYLAKE_USER_ID")
        self.timeout = timeout

        if mock is None:
            mock = os.getenv("MEMORYLAKE_MOCK", "").lower() in ("1", "true", "yes")
        self.mock = bool(mock)

        self._session_id: Optional[str] = None
        self._discovered_tools: Optional[List[str]] = None
        # In-memory store used only in mock mode
        self._mock_store: Dict[str, SharedMemory] = {}

        if self.mock:
            logger.info("MemoryLakeAdapter running in MOCK mode (MEMORYLAKE_MOCK=1)")
        else:
            logger.info("MemoryLakeAdapter configured for LIVE MCP at %s", self.mcp_url)

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
        """
        Write a memory in *proposed* (or explicitly given) status.
        Never silently upgrades to approved/verified.
        """
        if isinstance(memory_type, str):
            memory_type = MemoryType(memory_type)
        if isinstance(status, str):
            status = MemoryStatus(status)

        # Hard rule: agents may only propose; promotion is owner/orchestrator action
        if status in (MemoryStatus.APPROVED, MemoryStatus.VERIFIED) and (
            created_by is None or not str(created_by).startswith("user")
        ):
            logger.warning(
                "Refusing to write status=%s without user-owned created_by; forcing proposed",
                status.value,
            )
            status = MemoryStatus.PROPOSED

        scope = MemoryScope(
            user_id=user_id or self.default_user_id,
            project_id=project_id or self.default_project_id,
            task_id=task_id,
        )
        if not scope.project_id:
            raise ValueError("project_id is required for MemoryLake writes (project scope)")

        now = datetime.now(timezone.utc).isoformat()
        mem = SharedMemory(
            memory_id=str(uuid.uuid4()),
            memory_type=memory_type,
            content=content,
            scope=scope,
            status=status,
            source_ref=source_ref,
            evidence_ref=list(evidence_ref or []),
            created_by=created_by or "agent",
            created_at=now,
            confidence=float(confidence),
            version=1,
        )

        if self.mock:
            self._mock_store[mem.memory_id] = mem
            mem.remote_id = f"mock-{mem.memory_id}"
            return mem

        payload = self._to_add_payload(mem)
        result = self._call_tool(self.TOOL_ADD, payload)
        mem.raw = result if isinstance(result, dict) else {"result": result}
        mem.remote_id = self._extract_remote_id(result) or mem.memory_id
        return mem

    def search(
        self,
        query: str,
        *,
        project_id: Optional[str] = None,
        memory_type: Optional[MemoryType | str] = None,
        status: Optional[MemoryStatus | str] = None,
        top_k: int = 8,
    ) -> List[MemorySearchHit]:
        """Semantic search scoped to a project when project_id is provided."""
        project_id = project_id or self.default_project_id
        if isinstance(memory_type, str):
            memory_type = MemoryType(memory_type)
        if isinstance(status, str):
            status = MemoryStatus(status)

        if self.mock:
            return self._mock_search(query, project_id, memory_type, status, top_k)

        args: Dict[str, Any] = {"query": query, "top_k": top_k}
        if project_id:
            args["project_id"] = project_id
        if memory_type:
            args["memory_type"] = memory_type.value
        if status:
            args["status"] = status.value

        result = self._call_tool(self.TOOL_SEARCH, args)
        return self._parse_search_result(result, project_id=project_id)

    def detect_conflicts(
        self,
        query: Optional[str] = None,
        *,
        project_id: Optional[str] = None,
    ) -> ConflictReport:
        """Surface conflicting memories; never auto-resolve."""
        project_id = project_id or self.default_project_id
        if self.mock:
            return ConflictReport(conflicts=[], query=query, mock=True)

        args: Dict[str, Any] = {}
        if query:
            args["query"] = query
        if project_id:
            args["project_id"] = project_id
        result = self._call_tool(self.TOOL_DETECT_CONFLICTS, args)
        conflicts: List[Dict[str, Any]] = []
        if isinstance(result, dict):
            conflicts = list(result.get("conflicts") or result.get("items") or [])
        elif isinstance(result, list):
            conflicts = result
        return ConflictReport(conflicts=conflicts, query=query, mock=False)

    def list_resources(self) -> List[Dict[str, Any]]:
        """MCP resources/list — returns opaque resource descriptors."""
        if self.mock:
            return [
                {
                    "uri": f"memorylake://mock/{m.memory_id}",
                    "name": m.memory_id,
                    "description": m.content[:120],
                    "mimeType": "application/json",
                }
                for m in self._mock_store.values()
            ]
        return self._resources_list()

    def read_resource(self, uri: str) -> Dict[str, Any]:
        """MCP resources/read."""
        if self.mock:
            mid = uri.rstrip("/").split("/")[-1]
            mem = self._mock_store.get(mid)
            if not mem:
                raise KeyError(f"mock resource not found: {uri}")
            return {"uri": uri, "contents": [mem.to_contract_dict()]}
        return self._resources_read(uri)

    def promote(
        self,
        memory_id: str,
        new_status: MemoryStatus | str,
        *,
        actor: str,
        reason: Optional[str] = None,
    ) -> SharedMemory:
        """
        Explicit promotion path. Only allowed for user/owner actors.
        Agents must not call this for self-promotion.
        """
        if isinstance(new_status, str):
            new_status = MemoryStatus(new_status)
        if not str(actor).startswith(("user", "owner", "orchestrator")):
            raise PermissionError(
                "Only user/owner/orchestrator may promote memory status; "
                f"got actor={actor!r}"
            )
        if self.mock:
            mem = self._mock_store.get(memory_id)
            if not mem:
                raise KeyError(memory_id)
            mem.status = new_status
            mem.version += 1
            if reason:
                mem.raw["promotion_reason"] = reason
            return mem
        # Live: MemoryLake may not expose a dedicated "promote" tool.
        # Documented surface only has add_memory / search / detect_conflicts.
        # We therefore re-add with updated status and supersedes link when possible,
        # and leave a clear limitation note for the caller.
        raise NotImplementedError(
            "Live promotion requires a MemoryLake write path that accepts status "
            "updates. Current public MCP tools (add_memory / search_memory / "
            "detect_conflicts) do not document an update/promote tool. "
            "Re-propose with supersedes=old_id after owner review, or use REST API."
        )

    # ------------------------------------------------------------------
    # Discovery & health
    # ------------------------------------------------------------------

    def discover_tools(self) -> List[str]:
        """Call MCP tools/list if available; cache names."""
        if self.mock:
            self._discovered_tools = [self.TOOL_SEARCH, self.TOOL_ADD, self.TOOL_DETECT_CONFLICTS]
            return list(self._discovered_tools)
        # Best-effort; many HTTP MCP servers expose tools/list via JSON-RPC
        try:
            resp = self._jsonrpc("tools/list", {})
            tools = []
            if isinstance(resp, dict):
                for t in resp.get("tools") or []:
                    name = t.get("name") if isinstance(t, dict) else None
                    if name:
                        tools.append(name)
            self._discovered_tools = tools or [
                self.TOOL_SEARCH,
                self.TOOL_ADD,
                self.TOOL_DETECT_CONFLICTS,
            ]
        except Exception as e:
            logger.warning("tools/list failed (%s); falling back to documented names", e)
            self._discovered_tools = [
                self.TOOL_SEARCH,
                self.TOOL_ADD,
                self.TOOL_DETECT_CONFLICTS,
            ]
        return list(self._discovered_tools)

    def health(self) -> Dict[str, Any]:
        """Lightweight status for ops / tests."""
        return {
            "mock": self.mock,
            "mcp_url": self.mcp_url,
            "has_token": bool(self.token),
            "default_project_id": self.default_project_id,
            "discovered_tools": self._discovered_tools,
            "live_ready": not self.mock,
        }

    # ------------------------------------------------------------------
    # Internal — payload mapping & transport
    # ------------------------------------------------------------------

    def _to_add_payload(self, mem: SharedMemory) -> Dict[str, Any]:
        """
        Map Spec contract → documented add_memory arguments.
        Exact schema is not fully published; we send a rich content blob
        plus structured metadata so the server can index it.
        """
        return {
            "content": mem.content,
            "metadata": {
                "memory_id": mem.memory_id,
                "memory_type": mem.memory_type.value,
                "status": mem.status.value,
                "scope": mem.scope.as_dict(),
                "source_ref": mem.source_ref,
                "evidence_ref": mem.evidence_ref,
                "created_by": mem.created_by,
                "created_at": mem.created_at,
                "confidence": mem.confidence,
                "version": mem.version,
                "supersedes": mem.supersedes,
                "conflicts_with": mem.conflicts_with,
                "expires_at": mem.expires_at,
            },
            # Common optional fields seen in similar memory MCP servers
            "project_id": mem.scope.project_id,
            "type": mem.memory_type.value,
        }

    def _parse_search_result(
        self, result: Any, project_id: Optional[str] = None
    ) -> List[MemorySearchHit]:
        hits: List[MemorySearchHit] = []
        items: List[Any] = []
        if isinstance(result, dict):
            items = result.get("memories") or result.get("results") or result.get("items") or []
        elif isinstance(result, list):
            items = result

        for item in items:
            if not isinstance(item, dict):
                continue
            # Prefer nested metadata if present
            meta = item.get("metadata") or item
            content = item.get("content") or meta.get("content") or ""
            mid = meta.get("memory_id") or item.get("id") or str(uuid.uuid4())
            try:
                mtype = MemoryType(meta.get("memory_type") or meta.get("type") or "fact")
            except ValueError:
                mtype = MemoryType.FACT
            try:
                status = MemoryStatus(meta.get("status") or "proposed")
            except ValueError:
                status = MemoryStatus.PROPOSED
            scope_data = meta.get("scope") or {}
            if project_id and not scope_data.get("project_id"):
                scope_data["project_id"] = project_id
            mem = SharedMemory(
                memory_id=str(mid),
                memory_type=mtype,
                content=content,
                scope=MemoryScope(
                    user_id=scope_data.get("user_id"),
                    project_id=scope_data.get("project_id"),
                    task_id=scope_data.get("task_id"),
                ),
                status=status,
                source_ref=meta.get("source_ref"),
                evidence_ref=list(meta.get("evidence_ref") or []),
                created_by=meta.get("created_by"),
                created_at=meta.get("created_at"),
                confidence=float(meta.get("confidence") or 0.0),
                version=int(meta.get("version") or 1),
                remote_id=item.get("id") or meta.get("remote_id"),
                raw=item,
            )
            score = item.get("score") or item.get("similarity")
            snippet = item.get("snippet") or (content[:200] if content else None)
            hits.append(MemorySearchHit(memory=mem, score=score, snippet=snippet))
        return hits

    def _extract_remote_id(self, result: Any) -> Optional[str]:
        if isinstance(result, dict):
            return (
                result.get("id")
                or result.get("memory_id")
                or (result.get("data") or {}).get("id")
            )
        return None

    def _mock_search(
        self,
        query: str,
        project_id: Optional[str],
        memory_type: Optional[MemoryType],
        status: Optional[MemoryStatus],
        top_k: int,
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
                # simple token overlap
                if not any(tok in mem.content.lower() for tok in q.split() if len(tok) > 2):
                    continue
            hits.append(MemorySearchHit(memory=mem, score=0.9, snippet=mem.content[:200]))
        return hits[:top_k]

    # ------------------------------------------------------------------
    # Transport (JSON-RPC over HTTP; Streamable HTTP MCP style)
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _jsonrpc(self, method: str, params: Dict[str, Any]) -> Any:
        """Minimal JSON-RPC 2.0 POST. Raises on transport / protocol errors."""
        import urllib.error
        import urllib.request

        body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params or {},
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.mcp_url,
            data=data,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"MemoryLake MCP HTTP {e.code}: {err_body}") from e
        except Exception as e:
            raise RuntimeError(f"MemoryLake MCP transport error: {e}") from e

        # Handle possible SSE / multi-line; take last JSON object if needed
        text = raw.strip()
        if "data:" in text:
            # crude SSE extraction
            for line in reversed(text.splitlines()):
                line = line.strip()
                if line.startswith("data:"):
                    text = line[5:].strip()
                    break
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"MemoryLake MCP non-JSON response: {text[:300]}") from e

        if isinstance(payload, dict) and payload.get("error"):
            err = payload["error"]
            raise RuntimeError(f"MemoryLake MCP error: {err}")
        if isinstance(payload, dict) and "result" in payload:
            return payload["result"]
        return payload

    def _call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        # Standard MCP tools/call shape
        return self._jsonrpc(
            "tools/call",
            {"name": name, "arguments": arguments},
        )

    def _resources_list(self) -> List[Dict[str, Any]]:
        result = self._jsonrpc("resources/list", {})
        if isinstance(result, dict):
            return list(result.get("resources") or [])
        if isinstance(result, list):
            return result
        return []

    def _resources_read(self, uri: str) -> Dict[str, Any]:
        result = self._jsonrpc("resources/read", {"uri": uri})
        if isinstance(result, dict):
            return result
        return {"uri": uri, "contents": result}


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