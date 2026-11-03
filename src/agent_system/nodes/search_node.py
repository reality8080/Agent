"""Search node — reuses TavilySearchAdapter. Never treats mock as real evidence."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def search_node(state: Dict[str, Any]) -> Dict[str, Any]:
    query = (
        state.get("search_query")
        or state.get("user_request")
        or ""
    )[:500]

    if not query.strip():
        return {
            "search_results": [],
            "search_mock": True,
            "events": [{"type": "search", "query": "", "hits": 0, "mock": True}],
        }

    try:
        from adapters.search_adapter import TavilySearchAdapter

        adapter = TavilySearchAdapter()
        resp = adapter.search(query)
        results: List[Dict[str, Any]] = [
            {
                "title": r.title,
                "url": r.url,
                "content": r.content,
                "score": r.score,
            }
            for r in resp.results
        ]
        is_mock = bool(resp.mock)
        return {
            "search_query": query,
            "search_results": results,
            "search_mock": is_mock,
            "events": [
                {
                    "type": "search",
                    "query": query,
                    "hits": len(results),
                    "mock": is_mock,
                    # Explicit flag so downstream never confuses mock with evidence
                    "evidence_valid": not is_mock,
                }
            ],
        }
    except Exception as exc:
        logger.warning("Search failed: %s", exc)
        return {
            "search_query": query,
            "search_results": [],
            "search_mock": True,
            "events": [
                {
                    "type": "search",
                    "query": query,
                    "error": str(exc),
                    "mock": True,
                    "evidence_valid": False,
                }
            ],
        }
