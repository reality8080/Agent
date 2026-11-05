"""Project-scoped MemoryLake retrieval before planning."""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def memory_retrieval_node(state: Dict[str, Any]) -> Dict[str, Any]:
    from agent_system.runtime import get_memorylake_adapter
    project_id = __import__("os").getenv("MEMORYLAKE_PROJECT_ID")
    if not project_id:
        return {"memory_results": [], "memory_refs": [], "memory_mock": False,
                "memory_status": "skipped_no_project_scope",
                "events": [{"type": "memory_retrieval", "status": "skipped", "reason": "no_project_id"}]}
    try:
        from adapters.memorylake_adapter import MemoryStatus
        adapter = get_memorylake_adapter()
        hits: List[Dict[str, Any]] = []
        # Project filter is sent to the service and checked again on returned metadata.
        for status in (MemoryStatus.VERIFIED, MemoryStatus.APPROVED, MemoryStatus.DISPUTED):
            for hit in adapter.search(state.get("user_request", ""), project_id=project_id,
                                      status=status, top_k=4):
                mem = hit.memory
                # Enforce status locally as well as in the remote query. Some MCP
                # servers may not expose a status filter in search_memory schema.
                if mem.status != status:
                    continue
                if mem.scope.project_id != project_id:
                    continue
                # The legacy adapter fills a missing response project_id from the
                # request argument. For LIVE results, require the raw service item
                # itself to declare the project scope before treating it as memory.
                # Mock objects carry their scope directly in the local test store.
                if not adapter.mock:
                    raw_item = mem.raw if isinstance(mem.raw, dict) else {}
                    raw_meta = raw_item.get("metadata") if isinstance(raw_item.get("metadata"), dict) else raw_item
                    raw_scope = raw_meta.get("scope") if isinstance(raw_meta.get("scope"), dict) else {}
                    raw_project_id = (raw_scope.get("project_id") or raw_meta.get("project_id")
                                      or raw_item.get("project_id"))
                    if raw_project_id != project_id:
                        logger.warning("Ignoring MemoryLake result without an explicit matching project scope")
                        continue
                hits.append({
                    "memory_id": mem.memory_id, "memory_type": mem.memory_type.value,
                    "content": mem.content, "status": mem.status.value,
                    "project_id": mem.scope.project_id, "task_id": mem.scope.task_id,
                    "source_ref": mem.source_ref, "evidence_ref": list(mem.evidence_ref),
                    "created_by": mem.created_by, "created_at": mem.created_at,
                    "confidence": mem.confidence, "version": mem.version,
                    "score": hit.score, "snippet": hit.snippet,
                    "supersedes": mem.supersedes, "conflicts_with": list(mem.conflicts_with),
                    "expires_at": mem.expires_at,
                })
        conflicts = adapter.detect_conflicts(query=state.get("user_request", ""), project_id=project_id)
        # Mock data is context for offline tests only, never represented as live evidence.
        mock = bool(adapter.mock)
        return {"memory_results": hits, "memory_refs": [h["memory_id"] for h in hits],
                "memory_conflicts": list(conflicts.conflicts), "memory_mock": mock,
                "memory_status": "mock" if mock else "live",
                "events": [{"type": "memory_retrieval", "status": "completed", "hits": len(hits),
                            "mock": mock, "project_id": project_id,
                            "conflicts": len(conflicts.conflicts)}]}
    except Exception as exc:
        logger.warning("MemoryLake retrieval unavailable: %s", type(exc).__name__)
        return {"memory_results": [], "memory_refs": [], "memory_mock": False,
                "memory_status": "failed", "memory_error": type(exc).__name__,
                "events": [{"type": "memory_retrieval", "status": "failed",
                            "error_type": type(exc).__name__}]}
