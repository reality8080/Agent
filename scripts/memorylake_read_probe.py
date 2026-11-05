#!/usr/bin/env python3
"""Read-only MemoryLake inspect + project-scoped fact-search verification."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
except ImportError:
    pass

from adapters.memorylake_adapter import MemoryLakeAdapter


def _keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _keys(child) for key, child in list(value.items())[:20]}
    if isinstance(value, list):
        return [_keys(value[0])] if value else []
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return type(value).__name__
    return type(value).__name__


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="Agent System project context")
    parser.add_argument("--top-n", type=int, default=5)
    args = parser.parse_args()

    if os.getenv("MEMORYLAKE_MOCK", "0").strip().lower() in {"1", "true", "yes"}:
        print("BLOCKED: MEMORYLAKE_MOCK is enabled; set MEMORYLAKE_MOCK=0 for live read verification.")
        return 2

    adapter = MemoryLakeAdapter(mock=False)
    project_id = adapter.default_project_id
    print("MCP endpoint:", adapter.mcp_url)
    print("Project ID configured:", bool(project_id))
    print("Query:", args.query)
    print("READ_ONLY: true (inspect + search only; no remember/forget)")
    if not project_id:
        print("RESULT: BLOCKED_NO_PROJECT_ID")
        return 2

    try:
        tools = adapter.discover_tools()
        print("DISCOVERED_TOOLS:", tools)
        inspect_data = adapter.inspect()
        project_ids = sorted(adapter._extract_project_ids(inspect_data))
        print("INSPECT_TOP_LEVEL_KEYS:", list(inspect_data.keys()) if isinstance(inspect_data, dict) else type(inspect_data).__name__)
        print("BOUNDARY_PROJECT_IDS:", project_ids)
        print("CONFIGURED_PROJECT_IN_BOUNDARY:", project_id in project_ids)
        print("INSPECT_SHAPE:", json.dumps(_keys(inspect_data), ensure_ascii=False)[:5000])
        if project_id not in project_ids:
            print("RESULT: BLOCKED_PROJECT_OUTSIDE_BOUNDARY")
            print("HEALTH:", json.dumps(adapter.health(), ensure_ascii=False))
            return 3

        hits = adapter.search(args.query, project_id=project_id, top_k=args.top_n)
        print("SEARCH_HITS:", len(hits))
        for hit in hits[:args.top_n]:
            raw = hit.memory.raw if isinstance(hit.memory.raw, dict) else {}
            scope = raw.get("scope")
            if scope is None and isinstance(raw.get("metadata"), dict):
                scope = raw["metadata"].get("scope")
            print(json.dumps({
                "memory_id": hit.memory.memory_id,
                "project_id": hit.memory.scope.project_id,
                "returned_scope": scope,
                "status": hit.memory.status.value if hit.memory.status else None,
                "content_length": len(hit.memory.content),
                "raw_keys": sorted(raw.keys()),
            }, ensure_ascii=False))

        print("SEARCH_DIAGNOSTICS:", json.dumps(adapter._last_search_diagnostics, ensure_ascii=False))
        health = adapter.health()
        print("HEALTH:", json.dumps(health, ensure_ascii=False))
        if not health.get("ready_for_retrieval"):
            print("RESULT: FAIL_READ_NOT_VERIFIED")
            return 1
        print("RESULT: READ_VERIFIED" if hits else "RESULT: READ_VERIFIED_ZERO_MATCHES")
        return 0
    except Exception as exc:
        print("RESULT: FAILED")
        print("ERROR_TYPE:", type(exc).__name__)
        print("ERROR:", str(exc)[:800])
        print("HEALTH:", json.dumps(adapter.health(), ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
