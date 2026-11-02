"""
LangChain Tavily Search Adapter for Agent System v0.5 Layer 1.

- Uses langchain_tavily.TavilySearch (preferred import).
- Returns structured results with URL, title, content for Planner evidence.
- Supports mock mode when TAVILY_API_KEY is absent.
"""

from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """Single search hit kept for evidence / citation."""

    title: str
    url: str
    content: str
    score: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResponse:
    query: str
    results: List[SearchResult]
    mock: bool = False


class TavilySearchAdapter:
    """
    Thin adapter around langchain_tavily.TavilySearch.

    Configuration via environment:
      TAVILY_API_KEY          – required for live calls
      SEARCH_MAX_RESULTS      – default 5 (Planner-friendly)
      SEARCH_MOCK             – force mock (1/true)
    """

    def __init__(
        self,
        max_results: Optional[int] = None,
        mock: Optional[bool] = None,
        include_raw_content: bool = True,
    ):
        self.max_results = max_results or int(os.getenv("SEARCH_MAX_RESULTS", "5"))
        if mock is None:
            mock = os.getenv("SEARCH_MOCK", "").lower() in ("1", "true", "yes")
            if not mock and not os.getenv("TAVILY_API_KEY"):
                mock = True
        self.mock = mock
        self.include_raw_content = include_raw_content
        self._tool = None  # lazy

    def _get_tool(self):
        if self._tool is not None:
            return self._tool
        try:
            # Preferred import per task specification
            from langchain_tavily import TavilySearch
        except ImportError:
            # Fallback for older package layouts
            try:
                from langchain_community.tools.tavily_search import TavilySearchResults as TavilySearch
            except ImportError as e:
                raise ImportError(
                    "langchain-tavily is required. Install with: "
                    "pip install langchain-tavily"
                ) from e

        # TavilySearch (langchain_tavily) accepts max_results etc.
        self._tool = TavilySearch(
            max_results=self.max_results,
            # include_answer=False keeps focus on source documents
            include_answer=False,
            include_raw_content=self.include_raw_content,
        )
        return self._tool

    def search(self, query: str, max_results: Optional[int] = None) -> SearchResponse:
        """
        Execute a web search and return structured evidence.

        Never logs the API key. In mock mode returns deterministic placeholder results.
        """
        if not query or not query.strip():
            return SearchResponse(query=query or "", results=[], mock=self.mock)

        k = max_results or self.max_results

        if self.mock:
            return self._mock_search(query, k)

        tool = self._get_tool()
        try:
            # langchain_tavily.TavilySearch is a BaseTool; invoke with dict or str
            raw = tool.invoke({"query": query})
        except Exception as exc:
            logger.error("Tavily search failed: %s", type(exc).__name__)
            raise

        results = self._normalize(raw, limit=k)
        return SearchResponse(query=query, results=results, mock=False)

    def _normalize(self, raw: Any, limit: int) -> List[SearchResult]:
        """Convert various Tavily / LangChain return shapes into SearchResult list."""
        items: List[Dict[str, Any]] = []

        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            # Common keys: results, organic, data
            for key in ("results", "organic", "data"):
                if key in raw and isinstance(raw[key], list):
                    items = raw[key]
                    break
            else:
                items = [raw]
        elif isinstance(raw, str):
            # Some wrappers return a formatted string; treat as single content
            return [
                SearchResult(
                    title="(raw text result)",
                    url="",
                    content=raw[:2000],
                )
            ]
        else:
            logger.warning("Unexpected Tavily response type: %s", type(raw))
            return []

        out: List[SearchResult] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("name") or ""
            url = item.get("url") or item.get("link") or ""
            content = (
                item.get("content")
                or item.get("raw_content")
                or item.get("snippet")
                or item.get("description")
                or ""
            )
            score = item.get("score")
            out.append(
                SearchResult(
                    title=str(title),
                    url=str(url),
                    content=str(content)[:4000],  # keep evidence readable
                    score=float(score) if score is not None else None,
                    raw=item,
                )
            )
        return out

    def _mock_search(self, query: str, k: int) -> SearchResponse:
        """Deterministic mock results for offline / no-key testing."""
        results = []
        for i in range(min(k, 3)):
            results.append(
                SearchResult(
                    title=f"[MOCK] Result {i+1} for: {query[:60]}",
                    url=f"https://example.com/mock/{i+1}",
                    content=(
                        f"This is mock search content #{i+1} related to '{query}'. "
                        "Replace TAVILY_API_KEY and set SEARCH_MOCK=0 for live results. "
                        "Planner should treat this as unverified evidence (level D)."
                    ),
                    score=0.9 - i * 0.1,
                    raw={"mock": True, "index": i},
                )
            )
        return SearchResponse(query=query, results=results, mock=True)


def create_search_adapter(mock: Optional[bool] = None) -> TavilySearchAdapter:
    return TavilySearchAdapter(mock=mock)
