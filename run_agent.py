#!/usr/bin/env python3
"""CLI entrypoint for Agent System v0.5."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

def main():
    parser = argparse.ArgumentParser(description="Agent System v0.5")
    parser.add_argument("request", nargs="?", default="Write a hello module under workspace/")
    parser.add_argument("--thread-id", default=None)
    parser.add_argument("--env-id", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    # Sensible mock defaults for first run
    os.environ.setdefault("LLM_MOCK", "1")
    os.environ.setdefault("LAYA_MOCK", "1")
    os.environ.setdefault("SEARCH_MOCK", "1")
    os.environ.setdefault("GATE1_AUTO_APPROVE", "1")
    os.environ.setdefault("WORKSPACE_ROOT", str(ROOT / "workspace"))

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    from agent_system.graph import run_agent
    result = run_agent(
        args.request,
        thread_id=args.thread_id,
        environment_id=args.env_id,
    )
    print(json.dumps({
        "status": result.get("status"),
        "deliverable": result.get("deliverable"),
        "files_changed": result.get("files_changed"),
        "laya_task_type": result.get("laya_task_type"),
        "laya_confidence": result.get("laya_confidence"),
        "search_mock": result.get("search_mock"),
        "review_score": result.get("review_score"),
        "plan_version": result.get("plan_version"),
        "scope_hash": result.get("scope_hash"),
    }, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
