#!/usr/bin/env python3
"""Authorize MemoryLake MCP if needed and perform read-only tools/list discovery."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
except ImportError:
    pass

from adapters.memorylake_adapter import MemoryLakeAdapter


def main() -> int:
    if os.getenv("MEMORYLAKE_MOCK", "0").strip().lower() in {"1", "true", "yes"}:
        print("BLOCKED: MEMORYLAKE_MOCK is enabled; set MEMORYLAKE_MOCK=0 for Live OAuth discovery.")
        return 2

    adapter = MemoryLakeAdapter(mock=False)
    print("MCP endpoint:", adapter.mcp_url)
    print("OAuth callback port:", os.getenv("MEMORYLAKE_OAUTH_CALLBACK_PORT", "8765"))
    print("Project scope configured:", bool(adapter.default_project_id))
    print("No memory will be created, changed or deleted by this probe.")

    try:
        names = adapter.discover_tools()
    except Exception as exc:
        print("RESULT: FAILED")
        print("ERROR_TYPE:", type(exc).__name__)
        print("ERROR:", str(exc)[:800])
        print("HEALTH:", json.dumps(adapter.health(), indent=2, ensure_ascii=False))
        return 1

    print("RESULT: MCP_DISCOVERY_SUCCEEDED")
    print("TOOLS:")
    for name in names:
        schema = adapter.discovered_tool_schemas.get(name, {})
        properties = schema.get("properties") or {}
        print(json.dumps({
            "name": name,
            "description": schema.get("_tool_description"),
            "parameters": list(properties.keys()),
            "required": schema.get("required") or [],
        }, ensure_ascii=False))
    print("HEALTH:", json.dumps(adapter.health(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
