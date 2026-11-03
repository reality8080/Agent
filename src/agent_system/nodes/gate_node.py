"""Gate 1 / Gate 2 nodes.

Gate 1: human (or auto in mock) approval bound to plan_version + scope_hash + environment_id.
Gate 2: reviewer sign-off before Deliver.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict

from agent_system.security.approval import issue_approval_token

logger = logging.getLogger(__name__)


def gate1_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pause point for approval.
    In mock / auto-approve mode we issue a real token so downstream checks pass.
    In production this node would set status=waiting_approval and interrupt.
    """
    plan_version = state.get("plan_version") or "unknown"
    scope_hash = state.get("scope_hash") or "unknown"
    environment_id = state.get("environment_id") or "local-dev"

    auto = os.getenv("GATE1_AUTO_APPROVE", "1").lower() in ("1", "true", "yes")

    if not auto:
        # Real interrupt: LangGraph will pause if we raise or set interrupt
        return {
            "status": "waiting_approval",
            "gate1_approved": False,
            "events": [
                {
                    "type": "gate1_waiting",
                    "plan_version": plan_version,
                    "scope_hash": scope_hash,
                    "environment_id": environment_id,
                }
            ],
        }

    token = issue_approval_token(plan_version, scope_hash, environment_id)
    return {
        "gate1_approved": True,
        "gate1_approval_token": token.token,
        "gate1_approved_by": "auto",
        "gate1_approved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "gate1_issued_at": token.issued_at,  # internal for verifier
        "status": "running",
        "events": [
            {
                "type": "gate1_approved",
                "plan_version": plan_version,
                "scope_hash": scope_hash,
                "environment_id": environment_id,
                "approved_by": "auto",
            }
        ],
    }


def gate2_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Reviewer Gate 2 — simple pass if tests_passed and review_score >= threshold."""
    tests_passed = state.get("tests_passed", False)
    review_score = float(state.get("review_score") or 0.0)
    threshold = float(os.getenv("GATE2_SCORE_THRESHOLD", "0.6"))

    approved = bool(tests_passed and review_score >= threshold)
    return {
        "review_approved": approved,
        "status": "completed" if approved else "failed",
        "events": [
            {
                "type": "gate2",
                "approved": approved,
                "tests_passed": tests_passed,
                "review_score": review_score,
            }
        ],
    }
