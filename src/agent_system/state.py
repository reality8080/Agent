"""Typed LangGraph state for the Agent System integration."""
from __future__ import annotations

import operator
from typing import Annotated, Any, Dict, List, Optional, TypedDict


class AgentState(TypedDict, total=False):
    user_request: str
    task_id: str
    thread_id: str
    run_id: str
    trace_id: str
    environment_id: str
    workspace_root: str
    status: str
    error: Optional[str]

    # Laya shadow decision only; none of these fields drive workflow permissions.
    laya_shadow: bool
    laya_task_type: str
    laya_risk: str
    laya_priority: float
    laya_needs_approval: float
    laya_route: str
    laya_confidence: float
    laya_abstain: bool
    laya_model: str
    laya_route_reason: str
    laya_latency_ms: float
    laya_action: str
    laya_error: Optional[str]

    search_query: str
    search_results: List[Dict[str, Any]]
    search_mock: bool
    search_status: str
    search_error: Optional[str]
    repository_files: List[str]
    repository_context: List[Dict[str, Any]]
    repository_status: str

    memory_results: List[Dict[str, Any]]
    memory_refs: List[str]
    memory_mock: bool
    memory_status: str
    memory_conflicts: List[Dict[str, Any]]
    memory_conflicts_status: str
    memory_error: Optional[str]
    memory_hits_received: int
    memory_hits_eligible: int
    memory_rejected_unknown_status: int
    memory_proposal: Dict[str, Any]

    problem_brief: str
    evidence: List[Dict[str, Any]]
    plan: str
    plan_version: str
    plan_steps: List[Dict[str, Any]]
    privileged_actions: List[Dict[str, Any]]
    scope_paths: List[str]
    scope_hash: str
    plan_model: str
    plan_mock: bool
    plan_status: str
    main_model: str

    gate1_approved: bool
    gate1_approval_token: Optional[str]
    gate1_approved_by: Optional[str]
    gate1_approved_at: Optional[str]
    gate1_issued_at: Optional[float]
    gate1_rejection_reason: Optional[str]

    code_patches: List[Dict[str, Any]]
    code_model: str
    files_changed: List[str]
    coder_notes: str
    coding_mock: bool
    instruction_files_used: List[str]
    instruction_content: str

    git_available: bool
    git_status_before: str
    git_preexisting_files: List[str]
    git_tracked_files: List[str]
    git_file_hashes: Dict[str, Optional[str]]
    git_status_coder_start: str
    git_status_after: str
    git_diff: str
    git_changed_files: List[str]
    scope_violations: List[str]

    test_results: Dict[str, Any]
    tests_passed: bool
    test_output: str

    review: str
    review_score: float
    review_status: str
    review_findings: List[Dict[str, Any]]
    review_approved: bool

    privileged_actions: List[Dict[str, Any]]
    gate2_approved: bool
    deliverable: str

    events: Annotated[List[Dict[str, Any]], operator.add]
    messages: Annotated[List[Dict[str, Any]], operator.add]
    retry_count: int
    action_id: str
