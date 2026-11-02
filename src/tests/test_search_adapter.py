"""
Tests for TavilySearchAdapter.

  SEARCH_MOCK=1 python src/tests/test_search_adapter.py
  SEARCH_MOCK=0 python src/tests/test_search_adapter.py
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

_LOADED_ENV_FILES: list[str] = []
try:
    from dotenv import load_dotenv, dotenv_values

    for _base in list(dict.fromkeys(_CANDIDATE_ROOTS + [ROOT, Path.cwd()])):
        _env_path = _base / ".env"
        if _env_path.is_file():
            load_dotenv(_env_path, override=True)
            _LOADED_ENV_FILES.append(str(_env_path))
    load_dotenv(override=False)
except ImportError:
    dotenv_values = None  # type: ignore


from adapters.search_adapter import TavilySearchAdapter, create_search_adapter


def test_mock_search_returns_structured_results():
    adapter = TavilySearchAdapter(mock=True, max_results=3)
    resp = adapter.search("LangGraph human-in-the-loop")
    assert resp.mock is True
    assert resp.query == "LangGraph human-in-the-loop"
    assert 1 <= len(resp.results) <= 3
    for r in resp.results:
        assert r.title
        assert r.url.startswith("https://")
        assert r.content
        assert "mock" in r.content.lower() or r.raw.get("mock") is True
    print("MOCK PASS: Search adapter")


def test_empty_query():
    adapter = TavilySearchAdapter(mock=True)
    resp = adapter.search("")
    assert resp.results == []


def test_create_auto_mock_without_key():
    saved_key = os.environ.pop("TAVILY_API_KEY", None)
    saved_mock = os.environ.get("SEARCH_MOCK")
    try:
        os.environ.pop("SEARCH_MOCK", None)
        adapter = create_search_adapter()
        assert adapter.mock is True
    finally:
        if saved_key is not None:
            os.environ["TAVILY_API_KEY"] = saved_key
        # Restore SEARCH_MOCK so later live tests still see the CLI value
        if saved_mock is None:
            os.environ.pop("SEARCH_MOCK", None)
        else:
            os.environ["SEARCH_MOCK"] = saved_mock


def test_live_search():
    search_mock = os.getenv("SEARCH_MOCK", "1").lower() in ("1", "true", "yes")
    if search_mock:
        print("SKIP live search: SEARCH_MOCK is enabled (set SEARCH_MOCK=0)")
        return
    if not os.getenv("TAVILY_API_KEY"):
        print("SKIP live search: TAVILY_API_KEY not visible to os.getenv")
        print(f"  cwd={Path.cwd()}")
        print(f"  .env files loaded: {_LOADED_ENV_FILES or '(none)'}")
        if dotenv_values is not None:
            for path in _LOADED_ENV_FILES:
                data = dotenv_values(path) or {}
                print(f"  has TAVILY_API_KEY in {path}? {bool(data.get('TAVILY_API_KEY'))}")
                near = [k for k in data if "TAVILY" in k.upper()]
                if near:
                    print(f"  similar keys in file: {near}")
        return
    adapter = TavilySearchAdapter(mock=False, max_results=2)
    resp = adapter.search("LangGraph interrupt human approval")
    assert resp.mock is False
    assert len(resp.results) >= 1
    for r in resp.results:
        assert r.url
        assert r.title or r.content
    print(f"LIVE API PASS: Search returned {len(resp.results)} hits")


if __name__ == "__main__":
    print("--- SEARCH ENV ---")
    print(f"cwd={Path.cwd()}")
    print(f".env loaded={_LOADED_ENV_FILES or '(none)'}")
    print(f"SEARCH_MOCK={os.getenv('SEARCH_MOCK', '<unset>')!r}")
    tv = os.getenv("TAVILY_API_KEY")
    if tv:
        print(f"TAVILY_API_KEY present len={len(tv)}")
    else:
        print("TAVILY_API_KEY missing")
    print("------------------")
    test_mock_search_returns_structured_results()
    test_empty_query()
    test_create_auto_mock_without_key()
    test_live_search()
    print("Search tests finished.")