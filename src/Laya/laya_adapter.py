"""Minimal Laya adapter for Agent System Spec v0.5 Layer 1.
Shadow mode only. No auto-routing until eval passes.
"""
from __future__ import annotations
import time
from typing import Any, Dict, Optional
from dataclasses import dataclass, asdict

try:
    from laya import Router
except ImportError as e:
    raise ImportError("pip install laya  (and CPU torch first)") from e

# Schema phù hợp Spec: task_type, risk, priority, route, confidence/abstain
DEFAULT_TRIAGE_QUESTIONS = {
    "task_type": {
        "type": "choice",
        "instructions": "Classify the task type for the agent system.",
        "criteria": {
            "research": "needs web/docs/repo research or problem understanding",
            "planning": "needs architecture, alternatives or implementation plan",
            "coding": "needs source code change or patch",
            "review": "needs code/diff/test review only",
            "other": "unclear or out of scope",
        },
    },
    "risk": {
        "type": "choice",
        "instructions": "What is the risk level of acting on this task?",
        "criteria": {
            "low": "read-only or reversible, no secrets, no production",
            "medium": "writes source or runs tests in isolated workspace",
            "high": "destructive, install, SSH write, push, deploy, production",
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

@dataclass
class LayaDecision:
    task_type: str
    risk: str
    priority_score: float
    needs_approval: float  # noul probability
    confidence: float
    model_used: str
    route_reason: str
    abstain: bool
    latency_ms: float
    raw: Dict[str, Any]

    def to_langgraph_state(self) -> Dict[str, Any]:
        """Minimal payload for LangGraph node / checkpoint."""
        return {
            "laya_task_type": self.task_type,
            "laya_risk": self.risk,
            "laya_priority": self.priority_score,
            "laya_needs_approval": self.needs_approval,
            "laya_confidence": self.confidence,
            "laya_abstain": self.abstain,
            "laya_model": self.model_used,
            "laya_latency_ms": self.latency_ms,
            "laya_shadow": True,  # always true until eval gate passes
        }


class LayaTriageAdapter:
    def __init__(
        self,
        device: str = "cpu",
        model: Optional[str] = "multilingual",  # pin multilingual
        confidence_threshold: float = 0.55,
        max_loaded: int = 1,
    ):
        self.device = device
        self.model = model
        self.confidence_threshold = confidence_threshold
        self.router = Router(
            device=device,
            max_loaded=max_loaded,
            default="multilingual",
            preload=False,  # load on first use
        )

    def triage(self, state: Any, questions: Optional[Dict] = None) -> LayaDecision:
        questions = questions or DEFAULT_TRIAGE_QUESTIONS
        t0 = time.perf_counter()
        # Explicit model= to avoid unexpected English fallback on Latin-script Vietnamese
        result = self.router.predict(state, questions, model=self.model)
        latency = (time.perf_counter() - t0) * 1000

        answers = result.get("answers", {})
        routing = result.get("routing", {})

        # Extract with safe defaults
        task = answers.get("task_type", {}).get("choice", "other")
        risk = answers.get("risk", {}).get("choice", "medium")
        priority = float(answers.get("priority", {}).get("score", 0.5))
        needs_appr = float(answers.get("needs_approval", {}).get("noul", 0.5))

        # Simple confidence aggregate (mean of available confidences)
        confs = []
        for a in answers.values():
            if "confidence" in a:
                confs.append(float(a["confidence"]))
            elif "noul" in a:
                confs.append(max(float(a["noul"]), 1 - float(a["noul"])))
        confidence = sum(confs) / len(confs) if confs else 0.0
        abstain = confidence < self.confidence_threshold

        return LayaDecision(
            task_type=task,
            risk=risk,
            priority_score=priority,
            needs_approval=needs_appr,
            confidence=confidence,
            model_used=routing.get("model", self.model or "unknown"),
            route_reason=routing.get("reason", ""),
            abstain=abstain,
            latency_ms=latency,
            raw=result,
        )


# Shadow-only helper for LangGraph node
def shadow_triage_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Call from LangGraph; never mutates plan or grants write permission."""
    adapter = LayaTriageAdapter(device="cpu", model="multilingual")
    text = state.get("user_request") or state.get("task_text") or str(state)
    decision = adapter.triage(text)
    out = decision.to_langgraph_state()
    # Always keep human/rule path
    out["laya_recommendation"] = "abstain" if decision.abstain else decision.task_type
    return out