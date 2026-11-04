"""Laya Shadow node: typed recommendation only; never changes routing or permissions."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)


def laya_shadow_node(state: Dict[str, Any]) -> Dict[str, Any]:
    text = str(state.get("user_request") or "").strip()
    if not text:
        return {"laya_shadow": True, "laya_abstain": True, "laya_action": "log_only",
                "laya_confidence": 0.0, "laya_error": "empty_request",
                "events": [{"type": "laya_shadow", "action": "log_only", "abstain": True}]}

    if os.getenv("LAYA_MOCK", "").lower() in ("1", "true", "yes"):
        lower = text.lower()
        if any(k in lower for k in ("fix", "bug", "implement", "write", "code", "patch")):
            task_type, risk = "coding", "medium"
        elif any(k in lower for k in ("research", "search", "find", "what is")):
            task_type, risk = "research", "low"
        elif any(k in lower for k in ("plan", "design", "architecture")):
            task_type, risk = "planning", "low"
        elif any(k in lower for k in ("review", "audit")):
            task_type, risk = "review", "low"
        else:
            task_type, risk = "other", "low"
        decision = {"task_type": task_type, "risk": risk, "priority": 0.5,
                    "needs_approval": 0.5 if task_type == "coding" else 0.0,
                    "route": task_type, "confidence": 0.72, "abstain": False,
                    "model": "mock-laya", "reason": "deterministic_mock", "latency_ms": 0.0}
    else:
        try:
            from Laya.laya_shadow import laya_shadow_node as existing_laya_shadow
            raw = existing_laya_shadow({"user_request": text})
            decision = {
                "task_type": raw.get("laya_task_type", "other"),
                "risk": raw.get("laya_risk", "medium"),
                "priority": raw.get("laya_priority", 0.5),
                "needs_approval": raw.get("laya_needs_approval", 0.5),
                "route": raw.get("laya_route") or raw.get("route") or raw.get("laya_task_type", "other"),
                "confidence": raw.get("laya_confidence", 0.0),
                "abstain": raw.get("laya_abstain", True),
                "model": raw.get("laya_model", "unknown"),
                "reason": raw.get("laya_route_reason", ""),
                "latency_ms": raw.get("laya_latency_ms", 0.0),
            }
        except Exception as exc:
            logger.warning("Laya unavailable; shadow decision abstains: %s", type(exc).__name__)
            decision = {"task_type": "other", "risk": "unknown", "priority": 0.0,
                        "needs_approval": 0.0, "route": "human_or_rule", "confidence": 0.0,
                        "abstain": True, "model": "unavailable", "reason": "laya_error",
                        "latency_ms": 0.0, "error": type(exc).__name__}

    # Whitelist state fields: no upstream route, approval, permission or state mutation leaks through.
    decision["confidence"] = max(0.0, min(1.0, float(decision.get("confidence") or 0.0)))
    decision["priority"] = max(0.0, min(1.0, float(decision.get("priority") or 0.0)))
    decision["needs_approval"] = max(0.0, min(1.0, float(decision.get("needs_approval") or 0.0)))
    abstain = bool(decision.get("abstain", True))
    out = {
        "laya_shadow": True, "laya_task_type": str(decision["task_type"]),
        "laya_risk": str(decision["risk"]), "laya_priority": decision["priority"],
        "laya_needs_approval": decision["needs_approval"], "laya_route": str(decision["route"]),
        "laya_confidence": decision["confidence"], "laya_abstain": abstain,
        "laya_model": str(decision["model"]), "laya_route_reason": str(decision["reason"]),
        "laya_latency_ms": float(decision["latency_ms"] or 0.0),
        "laya_action": "log_only" if abstain else "suggest",
    }
    if decision.get("error"):
        out["laya_error"] = decision["error"]
    out["events"] = [{"type": "laya_shadow", "task_type": out["laya_task_type"],
                       "risk": out["laya_risk"], "priority": out["laya_priority"],
                       "route_suggestion": out["laya_route"], "confidence": out["laya_confidence"],
                       "abstain": abstain, "action": out["laya_action"]}]
    return out
