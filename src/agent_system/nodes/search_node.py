"""Tavily web search plus bounded, read-only repository retrieval."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List

from agent_system.workspace import get_workspace_root, normalize_relative_path

logger = logging.getLogger(__name__)
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}
_SOURCE_SUFFIXES = {".py", ".md", ".toml", ".yaml", ".yml", ".json", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".ps1", ".bat", ".sh", ".txt"}
_SECRET_NAME = re.compile(r"(^\.env|secret|credential|private|\.pem$|\.key$)", re.I)


def _repository_retrieval(request: str, workspace: Path) -> Dict[str, Any]:
    """Small standard-library retrieval; never leaves WORKSPACE_ROOT."""
    if not workspace.is_dir():
        return {"files": [], "context": [], "status": "workspace_missing"}
    terms = {w for w in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", request.lower())}
    ranked: List[tuple[int, str, str]] = []
    seen = 0
    for path in workspace.rglob("*"):
        if any(part in _SKIP_DIRS for part in path.relative_to(workspace).parts):
            continue
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        if _SECRET_NAME.search(path.name):
            continue
        seen += 1
        if seen > 1500:
            break
        try:
            if path.stat().st_size > 100_000:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            rel = normalize_relative_path(path.relative_to(workspace).as_posix(), strip_workspace_prefix=False)
        except (OSError, ValueError):
            continue
        low = content.lower()
        score = sum(1 for term in terms if term in path.name.lower()) * 3
        score += min(30, sum(1 for term in terms if term in low))
        # A useful low-score sample provides the repository shape when queries are broad.
        if score or len(ranked) < 4:
            ranked.append((score, rel, content))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    chosen = ranked[:8]
    return {
        "files": [rel for _, rel, _ in chosen],
        "context": [{"path": rel, "status": "local_workspace", "content": body[:1800]}
                    for _, rel, body in chosen],
        "status": "retrieved" if chosen else "empty",
    }


def search_node(state: Dict[str, Any]) -> Dict[str, Any]:
    query = str(state.get("search_query") or state.get("user_request") or "").strip()[:500]
    workspace = get_workspace_root(state.get("workspace_root"))
    repo = _repository_retrieval(str(state.get("user_request") or query), workspace)
    out: Dict[str, Any] = {"repository_files": repo["files"], "repository_context": repo["context"],
                           "repository_status": repo["status"]}
    if not query:
        out.update(search_results=[], search_mock=False, search_status="not_run",
                   events=[{"type": "search", "status": "not_run", "hits": 0,
                            "repository_files": repo["files"]}])
        return out
    try:
        from adapters.search_adapter import create_search_adapter
        response = create_search_adapter().search(query)
        mock = bool(response.mock)
        results: List[Dict[str, Any]] = [{
            "title": item.title, "url": item.url, "content": item.content,
            "score": item.score, "mock": mock, "evidence_valid": not mock,
            "source_status": "mock" if mock else "live",
        } for item in response.results]
        out.update({"search_query": query, "search_results": results, "search_mock": mock,
                    "search_status": "mock" if mock else "live",
                    "events": [{"type": "search", "query": query, "hits": len(results),
                                "mock": mock, "status": "completed", "evidence_valid": not mock,
                                "repository_files": repo["files"]}]})
        return out
    except Exception as exc:
        logger.warning("Tavily search unavailable: %s", type(exc).__name__)
        out.update({"search_query": query, "search_results": [], "search_mock": False,
                    "search_status": "failed", "search_error": type(exc).__name__,
                    "events": [{"type": "search", "query": query, "hits": 0,
                                "status": "failed", "error_type": type(exc).__name__,
                                "evidence_valid": False, "repository_files": repo["files"]}]})
        return out
