"""Laya Shadow node — recommendation only, never routes or grants permission."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Ensure src is on path
_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def laya_shadow_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Call existing Laya shadow triage.
    Only writes recommendation fields. Never sets gate1_approved or unlocks tools.
    """
    mock = os.getenv("LAYA_MOCK", "1").lower() in ("1", "true", "yes")

    text = (
        state.get("user_request")
        or state.get("task_text")
        or state.get("input")
        or ""
    )

    if not str(text).strip():
        return {
            "laya_shadow": True,
            "laya_abstain": True,
            "laya_action": "log_only",
            "laya_error": "empty_request",
            "events": [
                {
                    "type": "laya_shadow",
                    "action": "log_only",
                    "reason": "empty_request",
                }
            ],
        }

    if mock:
        # Deterministic mock that still respects Shadow contract
        lower = text.lower()
        if any(k in lower for k in ("fix", "bug", "implement", "write", "code", "patch")):
            task_type, risk = "coding", "medium"
        elif any(k in lower for k in ("research", "search", "find", "what is")):
            task_type, risk = "research", "low"
        elif any(k in lower for k in ("plan", "design", "architecture")):
            task_type, risk = "planning", "low"
        else:
            task_type, risk = "other", "low"

        return {
            "laya_shadow": True,
            "laya_task_type": task_type,
            "laya_risk": risk,
            "laya_priority": 0.6,
            "laya_needs_approval": 0.8 if task_type == "coding" else 0.2,
            "laya_confidence": 0.72,
            "laya_abstain": False,
            "laya_model": "mock-multilingual",
            "laya_route_reason": "mock_heuristic",
            "laya_latency_ms": 1.0,
            "laya_action": "suggest",
            "events": [
                {
                    "type": "laya_shadow",
                    "task_type": task_type,
                    "risk": risk,
                    "confidence": 0.72,
                    "action": "suggest",
                }
            ],
        }

    # Live path: reuse existing laya_shadow
    try:
        from Laya.laya_shadow import laya_shadow_node as _real

        out = _real(state)
        # Enforce shadow invariants even if upstream changes
        out["laya_shadow"] = True
        if "gate1_approved" in out:
            del out["gate1_approved"]
        return out
    except Exception as exc:
        logger.warning("Laya live call failed, falling back to mock: %s", exc)
        return {
            "laya_shadow": True,
            "laya_abstain": True,
            "laya_action": "log_only",
            "laya_error": str(exc),
            "laya_confidence": 0.0,
            "events": [{"type": "laya_shadow", "error": str(exc), "action": "log_only"}],
        }
