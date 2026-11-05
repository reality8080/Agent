"""Project-scoped, read-only MemoryLake retrieval before planning."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _explicit_project_id(item: Dict[str, Any]) -> str | None:
    """Extract scope only from returned metadata; never infer from the request."""
    candidates = [item]
    for key in ("metadata", "meta", "attributes", "fact"):
        value = item.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    for candidate in candidates:
        scope = candidate.get("scope")
        if isinstance(scope, str) and scope.startswith("project:"):
            return scope.split(":", 1)[1] or None
        if isinstance(scope, dict):
            value = scope.get("project_id") or scope.get("projectId")
            if value:
                return str(value)
        value = candidate.get("project_id") or candidate.get("projectId")
        if value:
            return str(value)
        project = candidate.get("project")
        if isinstance(project, dict):
            value = project.get("id") or project.get("project_id")
            if value:
                return str(value)
    return None


def _memory_payload(hit: Any) -> Dict[str, Any]:
    mem = hit.memory
    return {
        "memory_id": mem.memory_id,
        "memory_type": mem.memory_type.value,
        "content": mem.content,
        # None means the remote service did not expose a status; do not infer approval.
        "status": mem.status.value if mem.status else None,
        "project_id": mem.scope.project_id,
        "task_id": mem.scope.task_id,
        "source_ref": mem.source_ref,
        "evidence_ref": list(mem.evidence_ref),
        "created_by": mem.created_by,
        "created_at": mem.created_at,
        "confidence": mem.confidence,
        "version": mem.version,
        "score": hit.score,
        "snippet": hit.snippet,
        "supersedes": mem.supersedes,
        "conflicts_with": list(mem.conflicts_with),
        "expires_at": mem.expires_at,
    }


def memory_retrieval_node(state: Dict[str, Any]) -> Dict[str, Any]:
    project_id = os.getenv("MEMORYLAKE_PROJECT_ID")
    if not project_id:
        return {
            "memory_results": [], "memory_refs": [], "memory_mock": False,
            "memory_status": "skipped_no_project_scope",
            "memory_conflicts": [], "memory_conflicts_status": "not_checked",
            "memory_hits_received": 0, "memory_hits_eligible": 0,
            "memory_rejected_unknown_status": 0,
            "events": [{"type": "memory_retrieval", "status": "skipped", "reason": "no_project_id"}],
        }

    try:
        from adapters.memorylake_adapter import MemoryStatus
        from agent_system.runtime import get_memorylake_adapter

        adapter = get_memorylake_adapter()
        query = str(state.get("user_request") or "project context")
        if adapter.mock:
            # Keep mock semantics compatible with the local SharedMemory contract.
            hits = []
            for status in (MemoryStatus.VERIFIED, MemoryStatus.APPROVED, MemoryStatus.DISPUTED):
                hits.extend(adapter.search(query, project_id=project_id, status=status, top_k=4))
        else:
            # The live endpoint has no status filter. Its search API is fact-based;
            # the adapter first calls inspect and verifies project boundary scope.
            hits = adapter.search(query, project_id=project_id, top_k=12)

        results: List[Dict[str, Any]] = []
        rejected_unknown_status = 0
        for hit in hits:
            mem = hit.memory
            if mem.scope.project_id != project_id:
                continue
            # Live search has no status filter. Facts without an explicit
            # VERIFIED/APPROVED/DISPUTED status are not safe planner evidence.
            if mem.status is None:
                rejected_unknown_status += 1
                continue
            if mem.status in (MemoryStatus.PROPOSED, MemoryStatus.DEPRECATED):
                continue
            if not adapter.mock:
                # Defense in depth: never trust a requested project ID as returned provenance.
                raw_item = mem.raw if isinstance(mem.raw, dict) else {}
                raw_project_id = _explicit_project_id(raw_item)
                if raw_project_id != project_id:
                    logger.warning("Ignoring MemoryLake result without explicit matching project scope")
                    continue
            results.append(_memory_payload(hit))

        conflict_report = adapter.detect_conflicts(query=query, project_id=project_id)
        conflicts = list(conflict_report.conflicts) if conflict_report.available else []
        conflict_status = "available" if conflict_report.available else "unavailable"
        mock = bool(adapter.mock)
        return {
            "memory_results": results,
            "memory_refs": [item["memory_id"] for item in results],
            "memory_conflicts": conflicts,
            "memory_conflicts_status": conflict_status,
            "memory_mock": mock,
            "memory_status": "mock" if mock else "live_read_verified",
            "memory_hits_received": len(hits),
            "memory_hits_eligible": len(results),
            "memory_rejected_unknown_status": rejected_unknown_status,
            "events": [{
                "type": "memory_retrieval", "status": "completed", "hits": len(results),
                "hits_received": len(hits), "rejected_unknown_status": rejected_unknown_status,
                "mock": mock, "project_id": project_id,
                "conflict_check": conflict_status,
            }],
        }
    except Exception as exc:
        logger.warning("MemoryLake retrieval unavailable: %s", type(exc).__name__)
        return {
            "memory_results": [], "memory_refs": [], "memory_mock": False,
            "memory_conflicts": [], "memory_conflicts_status": "not_checked",
            "memory_status": "failed", "memory_error": type(exc).__name__,
            "memory_hits_received": 0, "memory_hits_eligible": 0,
            "memory_rejected_unknown_status": 0,
            "events": [{"type": "memory_retrieval", "status": "failed",
                        "error_type": type(exc).__name__}],
        }
