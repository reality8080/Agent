"""Deliver node — final package of results."""

from __future__ import annotations

from typing import Any, Dict


def deliver_node(state: Dict[str, Any]) -> Dict[str, Any]:
    approved = state.get("review_approved", False)
    files = state.get("files_changed") or []
    plan_version = state.get("plan_version", "")
    review_score = state.get("review_score", 0.0)

    if not approved:
        return {
            "status": "failed",
            "deliverable": (
                f"Delivery rejected (Gate 2). score={review_score}, "
                f"files={files}, plan={plan_version}"
            ),
            "events": [{"type": "deliver_rejected", "score": review_score}],
        }

    summary = (
        f"# Agent System Deliverable\n"
        f"- plan_version: {plan_version}\n"
        f"- files_changed: {files}\n"
        f"- review_score: {review_score}\n"
        f"- tests_passed: {state.get('tests_passed')}\n"
        f"- laya_task_type: {state.get('laya_task_type')}\n"
        f"- search_mock: {state.get('search_mock')}\n"
    )
    return {
        "status": "completed",
        "deliverable": summary,
        "events": [
            {
                "type": "delivered",
                "plan_version": plan_version,
                "files": files,
                "score": review_score,
            }
        ],
    }


def receive_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise inbound request and assign ids."""
    import uuid
    import os

    req = state.get("user_request") or state.get("input") or ""
    task_id = state.get("task_id") or str(uuid.uuid4())[:12]
    thread_id = state.get("thread_id") or task_id
    env_id = state.get("environment_id") or os.getenv("ENVIRONMENT_ID", "local-dev")

    return {
        "user_request": str(req).strip(),
        "task_id": task_id,
        "thread_id": thread_id,
        "environment_id": env_id,
        "status": "running",
        "retry_count": state.get("retry_count") or 0,
        "events": [
            {
                "type": "received",
                "task_id": task_id,
                "thread_id": thread_id,
                "environment_id": env_id,
            }
        ],
    }
