"""Planner node — Main LLM in PLAN mode (read-only tools only)."""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _scope_hash(paths: List[str]) -> str:
    from agent_system.security.approval import compute_scope_hash

    return compute_scope_hash(paths)


def planner_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Produce implementation plan. Never executes write tools."""
    from adapters.llm_adapter import LLMAdapter, LLMMode

    user_request = state.get("user_request", "")
    search_results = state.get("search_results") or []
    search_mock = state.get("search_mock", True)
    laya_type = state.get("laya_task_type", "other")

    # Build evidence summary (mark mock)
    evidence_lines = []
    if search_results and not search_mock:
        for r in search_results[:5]:
            evidence_lines.append(f"- {r.get('title')}: {r.get('url')}")
    elif search_mock:
        evidence_lines.append("(search returned mock data — not used as real evidence)")

    evidence_block = "\n".join(evidence_lines) or "(no external evidence)"

    system = (
        "You are the Planner for Agent System v0.5. "
        "You operate in PLAN mode only: produce a clear, step-by-step implementation plan. "
        "Do NOT call write tools. Do NOT generate code patches yet. "
        "List the exact file paths that will need to be modified (scope). "
        "Output format:\n"
        "PLAN:\n<numbered steps>\n"
        "SCOPE:\n- path1\n- path2\n"
        "RISKS:\n<short>\n"
    )

    user_msg = (
        f"Task type (Laya suggestion): {laya_type}\n"
        f"User request:\n{user_request}\n\n"
        f"Evidence:\n{evidence_block}\n"
    )

    from adapters.llm_adapter import ModelProvider
    prov_name = os.getenv("LLM_PROVIDER", "gemini").lower()
    try:
        prov = ModelProvider(prov_name)
    except ValueError:
        prov = ModelProvider.GEMINI
    adapter = LLMAdapter(
        default_provider=prov,
        mock=os.getenv("LLM_MOCK", "1").lower() in ("1", "true", "yes"),
    )

    try:
        resp = adapter.complete(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            mode=LLMMode.PLAN,
            task="plan",
        )
        plan_text = resp.content or ""
        model_used = resp.model
    except Exception as exc:
        logger.warning("Planner LLM failed: %s", exc)
        plan_text = (
            f"PLAN:\n1. Analyse request: {user_request[:200]}\n"
            "2. Implement minimal change in workspace.\n"
            "3. Run tests.\n"
            "SCOPE:\n- workspace/\n"
            "RISKS:\n- Low (isolated workspace)\n"
        )
        model_used = "fallback"

    # Parse crude SCOPE section
    scope_paths: List[str] = []
    if "SCOPE:" in plan_text:
        after = plan_text.split("SCOPE:", 1)[1]
        for line in after.splitlines():
            line = line.strip().lstrip("-* ").strip()
            if not line or line.upper().startswith("RISK"):
                if line.upper().startswith("RISK"):
                    break
                continue
            if line.endswith("/"):
                scope_paths.append(line)
            elif "/" in line or line.endswith((".py", ".md", ".txt", ".yaml", ".json")):
                scope_paths.append(line)

    if not scope_paths:
        scope_paths = ["workspace/"]

    plan_version = str(uuid.uuid4())[:8]
    scope_hash = _scope_hash(scope_paths)

    return {
        "plan": plan_text,
        "plan_version": plan_version,
        "plan_steps": [{"raw": plan_text}],
        "scope_paths": scope_paths,
        "scope_hash": scope_hash,
        "plan_model": model_used,
        "status": "waiting_approval",
        "events": [
            {
                "type": "plan_created",
                "plan_version": plan_version,
                "scope_hash": scope_hash,
                "scope_paths": scope_paths,
                "model": model_used,
            }
        ],
    }
