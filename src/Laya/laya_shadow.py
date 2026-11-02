# src/laya_shadow.py
"""Laya Layer-1 shadow triage for Agent System Spec v0.5.
- Decision model only (typed triage).
- Never grants write / never bypasses Gate 1.
- Output is suggestion + log fields for LangGraph state.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from laya import Router

# --- Schema khớp Spec (task type, risk, priority, approval signal) ---
TRIAGE_QUESTIONS: Dict[str, Any] = {
    "task_type": {
        "type": "choice",
        "instructions": "Classify the task for the agent system.",
        "criteria": {
            "research": "needs docs, web, repo research or problem understanding",
            "planning": "needs architecture, alternatives or implementation plan",
            "coding": "needs source change, patch or implementation",
            "review": "needs read-only review of code, diff or tests",
            "other": "unclear or out of agent scope",
        },
    },
    "risk": {
        "type": "choice",
        "instructions": "Risk level if the agent acts on this task.",
        "criteria": {
            "low": "read-only or easily reversible; no secrets; no production",
            "medium": "writes source or runs tests inside isolated workspace",
            "high": "destructive, system install, SSH write, push, deploy, production",
        },
    },
    "priority": {
        "type": "score",
        "instructions": "How urgent is this task?",
        "criteria": ["can wait", "normal", "blocking or time-critical"],
    },
    "needs_approval": {
        "type": "noul",
        "instructions": "Does this task require explicit user Gate approval before any write?",
    },
}

CONFIDENCE_THRESHOLD = 0.55  # below → abstain → human/rule path


@dataclass
class LayaDecision:
    task_type: str
    risk: str
    priority_score: float
    needs_approval: float
    confidence: float
    abstain: bool
    model_used: str
    route_reason: str
    latency_ms: float


class LayaTriageAdapter:
    """Thin wrapper. Loads multilingual checkpoint on first use. CPU only."""

    def __init__(
        self,
        device: str = "cpu",
        model: str = "multilingual",
        confidence_threshold: float = CONFIDENCE_THRESHOLD,
    ):
        self.model = model
        self.confidence_threshold = confidence_threshold
        self.router = Router(
            device=device,
            max_loaded=1,
            default="multilingual",
            preload=False,
        )

    def triage(self, text: str) -> LayaDecision:
        t0 = time.perf_counter()
        result = self.router.predict(text, TRIAGE_QUESTIONS, model=self.model)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        answers = result.get("answers") or {}
        routing = result.get("routing") or {}

        task_type = (answers.get("task_type") or {}).get("choice") or "other"
        risk = (answers.get("risk") or {}).get("choice") or "medium"
        priority = float((answers.get("priority") or {}).get("score") or 0.5)
        needs_approval = float((answers.get("needs_approval") or {}).get("noul") or 0.5)

        confs = []
        for a in answers.values():
            if not isinstance(a, dict):
                continue
            if "confidence" in a:
                confs.append(float(a["confidence"]))
            elif "noul" in a:
                p = float(a["noul"])
                confs.append(max(p, 1.0 - p))
        confidence = sum(confs) / len(confs) if confs else 0.0
        abstain = confidence < self.confidence_threshold

        return LayaDecision(
            task_type=task_type,
            risk=risk,
            priority_score=priority,
            needs_approval=needs_approval,
            confidence=confidence,
            abstain=abstain,
            model_used=str(routing.get("model") or self.model),
            route_reason=str(routing.get("reason") or ""),
            latency_ms=latency_ms,
        )


# Singleton nhẹ: tránh reload model mỗi node call trong cùng process
_ADAPTER: Optional[LayaTriageAdapter] = None


def _get_adapter() -> LayaTriageAdapter:
    global _ADAPTER
    if _ADAPTER is None:
        _ADAPTER = LayaTriageAdapter(device="cpu", model="multilingual")
    return _ADAPTER


def laya_shadow_node(state: dict) -> dict:
    """
    LangGraph node — SHADOW ONLY.

    Logic:
    1. Đọc request text từ state.
    2. Gọi Laya → typed decision + confidence.
    3. Nếu abstain hoặc conf thấp → route = human_or_rule, action = log_only.
    4. Nếu conf đủ → route = task_type gợi ý, action = suggest (KHÔNG execute).
    5. Mọi field chỉ ghi vào state; không mở write tool, không bỏ Gate 1.
    """
    text = (
        state.get("user_request")
        or state.get("task_text")
        or state.get("input")
        or ""
    )
    if not str(text).strip():
        return {
            **state,
            "laya_shadow": True,
            "laya_abstain": True,
            "laya_action": "log_only",
            "route": "human_or_rule",
            "laya_error": "empty_request",
        }

    d = _get_adapter().triage(str(text))

    # Hard fallback: low confidence → không tin model
    if d.abstain or d.confidence < CONFIDENCE_THRESHOLD:
        route = "human_or_rule"
        action = "log_only"
    else:
        route = d.task_type  # gợi ý duy nhất
        action = "suggest"

    return {
        **state,
        "laya_shadow": True,
        "laya_task_type": d.task_type,
        "laya_risk": d.risk,
        "laya_priority": d.priority_score,
        "laya_needs_approval": d.needs_approval,
        "laya_confidence": d.confidence,
        "laya_abstain": d.abstain,
        "laya_model": d.model_used,
        "laya_route_reason": d.route_reason,
        "laya_latency_ms": d.latency_ms,
        "laya_action": action,
        "route": route,
    }