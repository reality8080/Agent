"""Agent System v0.5 — Typed state for LangGraph workflow.

All nodes read/write this state. Laya only writes recommendation fields.
Approval is explicit and cryptographically bound (plan_version + scope_hash + environment_id).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict, Annotated
import operator


class AgentState(TypedDict, total=False):
    # --- Input / Receive ---
    user_request: str
    task_id: str
    thread_id: str
    environment_id: str

    # --- Laya Shadow (recommendation only) ---
    laya_shadow: bool
    laya_task_type: str
    laya_risk: str
    laya_priority: float
    laya_needs_approval: float
    laya_confidence: float
    laya_abstain: bool
    laya_model: str
    laya_route_reason: str
    laya_latency_ms: float
    laya_action: str  # "suggest" | "log_only"
    laya_error: Optional[str]

    # --- Search evidence ---
    search_query: str
    search_results: List[Dict[str, Any]]
    search_mock: bool

    # --- Planner (Plan mode, read-only tools) ---
    plan: str
    plan_version: str
    plan_steps: List[Dict[str, Any]]
    scope_paths: List[str]
    scope_hash: str
    plan_model: str

    # --- Gate 1 ---
    gate1_approved: bool
    gate1_approval_token: Optional[str]
    gate1_approved_by: Optional[str]
    gate1_approved_at: Optional[str]
    gate1_issued_at: Optional[float]
    gate1_rejection_reason: Optional[str]

    # --- Coder (Coding mode, only after Gate 1) ---
    code_patches: List[Dict[str, Any]]
    code_model: str
    files_changed: List[str]
    coder_notes: str

    # --- Test Runner ---
    test_results: Dict[str, Any]
    tests_passed: bool
    test_output: str

    # --- Reviewer ---
    review: str
    review_score: float
    review_approved: bool  # Gate 2

    # --- Deliver ---
    deliverable: str
    status: str  # "running" | "waiting_approval" | "completed" | "failed" | "aborted"
    error: Optional[str]

    # --- Audit / Memory ---
    events: Annotated[List[Dict[str, Any]], operator.add]
    memory_refs: List[str]

    # --- Internal control ---
    messages: Annotated[List[Dict[str, Any]], operator.add]
    next_action: str
    retry_count: int
    action_id: str
