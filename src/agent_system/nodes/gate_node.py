"""LangGraph interrupt/resume approval gates, bound to immutable plan scope."""
from __future__ import annotations

import time
from typing import Any, Dict

from agent_system.security.approval import compute_scope_hash, issue_approval_token


def gate1_node(state: Dict[str, Any]) -> Dict[str, Any]:
    plan_version = str(state.get("plan_version") or "")
    scope_hash = str(state.get("scope_hash") or "")
    environment_id = str(state.get("environment_id") or "")
    workspace_root = str(state.get("workspace_root") or "")
    scope_paths = list(state.get("scope_paths") or [])
    if not plan_version or not scope_hash or not environment_id or not scope_paths or not workspace_root:
        return {"status": "failed", "gate1_approved": False, "error": "Gate 1 binding is incomplete",
                "events": [{"type": "gate1_rejected", "reason": "missing_binding"}]}
    try:
        if compute_scope_hash(scope_paths, workspace_root) != scope_hash:
            raise ValueError("scope hash mismatch")
    except Exception as exc:
        return {"status": "failed", "gate1_approved": False,
                "error": f"Gate 1 scope validation failed: {type(exc).__name__}",
                "events": [{"type": "gate1_rejected", "reason": "scope_hash_mismatch"}]}

    # Gate 1 always requires a human interrupt. Development auto-approval is
    # intentionally disabled too: Coding permission is never inferred from config.
    from langgraph.types import interrupt
    decision = interrupt({
        "gate": "gate1", "approval_required": True, "action": "approve_plan_for_scoped_coding",
        "plan_version": plan_version, "scope_hash": scope_hash,
        "scope_paths": scope_paths,
        "environment_id": environment_id,
    })
    if not isinstance(decision, dict) or decision.get("approve") is not True:
        return {"status": "aborted", "gate1_approved": False,
                "gate1_rejection_reason": str((decision or {}).get("reason", "user_rejected")) if isinstance(decision, dict) else "invalid_decision",
                "events": [{"type": "gate1_rejected", "reason": "not_approved"}]}
    expected = {"plan_version": plan_version, "scope_hash": scope_hash,
                "environment_id": environment_id}
    if any(decision.get(k) != v for k, v in expected.items()):
        return {"status": "aborted", "gate1_approved": False,
                "gate1_rejection_reason": "stale_or_wrong_scope_approval",
                "events": [{"type": "gate1_rejected", "reason": "stale_or_wrong_scope_approval",
                            "provided_plan_version": decision.get("plan_version"),
                            "provided_scope_hash": decision.get("scope_hash"),
                            "provided_environment_id": decision.get("environment_id")}]}
    approved_by = str(decision.get("approved_by") or "user")

    try:
        token = issue_approval_token(plan_version, scope_hash, environment_id)
    except RuntimeError as exc:
        return {"status": "failed", "gate1_approved": False, "error": str(exc),
                "events": [{"type": "gate1_rejected", "reason": "approval_secret_not_configured"}]}
    approved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "gate1_approved": True, "gate1_approval_token": token.token,
        "gate1_approved_by": approved_by, "gate1_approved_at": approved_at,
        "gate1_issued_at": token.issued_at, "status": "running",
        "events": [{"type": "gate1_approved", "plan_version": plan_version,
                    "scope_hash": scope_hash, "environment_id": environment_id,
                    "approved_by": approved_by, "approval_expires_at": token.expires_at}],
    }


def gate2_node(state: Dict[str, Any]) -> Dict[str, Any]:
    actions = list(state.get("privileged_actions") or [])
    if not actions:
        return {"gate2_approved": True, "gate2_status": "skipped",
                "events": [{"type": "gate2_skipped", "reason": "no_privileged_actions"}]}
    try:
        if compute_scope_hash(state.get("scope_paths") or [], state.get("workspace_root", "")) != state.get("scope_hash"):
            raise ValueError("scope hash mismatch")
    except Exception as exc:
        return {"gate2_approved": False, "gate2_status": "rejected",
                "status": "failed", "error": f"Gate 2 binding invalid: {type(exc).__name__}",
                "events": [{"type": "gate2_rejected", "reason": "scope_hash_mismatch"}]}
    from langgraph.types import interrupt
    decision = interrupt({
        "gate": "gate2", "approval_required": True,
        "actions": actions, "plan_version": state.get("plan_version"),
        "scope_hash": state.get("scope_hash"), "environment_id": state.get("environment_id"),
    })
    expected = {"plan_version": state.get("plan_version"), "scope_hash": state.get("scope_hash"),
                "environment_id": state.get("environment_id")}
    valid = isinstance(decision, dict) and decision.get("approve") is True and all(
        decision.get(k) == v for k, v in expected.items()
    ) and decision.get("actions") == actions
    # This MVP gate records authorization only. No privileged-action executor is installed.
    return {"gate2_approved": bool(valid), "gate2_status": "approved_not_executed" if valid else "rejected",
            "status": "running" if valid else "aborted",
            "events": [{"type": "gate2_approved_not_executed" if valid else "gate2_rejected",
                        "actions": actions, "approved_by": (decision or {}).get("approved_by") if isinstance(decision, dict) else None,
                        "execution_status": "not_implemented" if valid else "not_authorized"}]}
