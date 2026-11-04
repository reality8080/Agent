"""Receive and Deliver nodes; memory is proposed only after delivery."""
from __future__ import annotations

import hashlib
import os
import uuid
from typing import Any, Dict

from agent_system.workspace import get_workspace_root, git_snapshot


def receive_node(state: Dict[str, Any]) -> Dict[str, Any]:
    request = str(state.get("user_request") or state.get("input") or "").strip()
    task_id = state.get("task_id") or str(uuid.uuid4())
    thread_id = state.get("thread_id") or task_id
    environment_id = state.get("environment_id") or os.getenv("ENVIRONMENT_ID", "local-dev")
    workspace = get_workspace_root(state.get("workspace_root"))
    snapshot = git_snapshot(workspace)
    run_id = state.get("run_id") or str(uuid.uuid4())
    trace_id = state.get("trace_id") or str(uuid.uuid4())
    return {
        "user_request": request, "task_id": task_id, "thread_id": thread_id,
        "run_id": run_id, "trace_id": trace_id, "environment_id": environment_id,
        "workspace_root": str(workspace), "status": "running", "retry_count": state.get("retry_count") or 0,
        "git_available": bool(snapshot.get("available")),
        "git_status_before": snapshot.get("status", ""),
        "git_preexisting_files": snapshot.get("files", []),
        "git_tracked_files": snapshot.get("tracked_files", []),
        "events": [{"type": "received", "task_id": task_id, "thread_id": thread_id,
                    "environment_id": environment_id, "git_available": bool(snapshot.get("available")),
                    "preexisting_dirty_count": len(snapshot.get("files", []))}],
    }


def _memory_proposal(state: Dict[str, Any]) -> Dict[str, Any]:
    project_id = os.getenv("MEMORYLAKE_PROJECT_ID")
    if not project_id:
        return {"status": "skipped_no_project_scope"}
    try:
        from adapters.memorylake_adapter import MemoryStatus, MemoryType
        from agent_system.runtime import get_memorylake_adapter
        adapter = get_memorylake_adapter()
        content = (
            f"Task outcome: {state.get('status')}\n"
            f"Plan version: {state.get('plan_version')}\n"
            f"Changed paths: {state.get('files_changed', [])}\n"
            f"Test status: {(state.get('test_results') or {}).get('status', 'not_run')}\n"
            f"Review status: {state.get('review_status', 'not_verified')}\n"
            f"Instruction files: {state.get('instruction_files_used', [])}"
        )
        conflicts = adapter.detect_conflicts(query=content, project_id=project_id)
        status = MemoryStatus.DISPUTED if conflicts.conflicts else MemoryStatus.PROPOSED
        diff_hash = hashlib.sha256((state.get("git_diff") or "").encode("utf-8")).hexdigest()
        evidence = [state.get("run_id", ""), diff_hash]
        memory = adapter.propose_memory(
            content=content, memory_type=MemoryType.REFLECTION, project_id=project_id,
            task_id=state.get("task_id"), source_ref=f"agent-run:{state.get('run_id')}",
            evidence_ref=[x for x in evidence if x], created_by="agent:orchestrator",
            confidence=0.5 if state.get("coding_mock") else 0.7, status=status,
        )
        return {"status": memory.status.value, "memory_id": memory.memory_id,
                "project_id": project_id, "source_ref": memory.source_ref,
                "conflicts": conflicts.conflicts, "mock": adapter.mock}
    except Exception as exc:
        return {"status": "failed", "error_type": type(exc).__name__}


def deliver_node(state: Dict[str, Any]) -> Dict[str, Any]:
    files = state.get("files_changed") or []
    tests = state.get("test_results") or {"status": "not_run"}
    review_status = state.get("review_status", "not_verified")
    privileged_execution_status = state.get("gate2_status")
    if state.get("coding_mock"):
        status = "dry_run"
    elif state.get("status") in ("failed", "aborted"):
        status = state.get("status")
    elif tests.get("status") == "passed" and review_status == "pass" and not state.get("scope_violations"):
        status = "completed"
    else:
        status = "completed_with_warnings"
    if privileged_execution_status == "approved_not_executed":
        status = "completed_with_warnings"
    summary = (
        "# Agent System Deliverable\n"
        f"- task_id: {state.get('task_id')}\n- run_id: {state.get('run_id')}\n"
        f"- status: {status}\n- plan_version: {state.get('plan_version')}\n"
        f"- scope_paths: {state.get('scope_paths', [])}\n- scope_hash: {state.get('scope_hash')}\n"
        f"- files_changed: {files}\n- test_status: {tests.get('status', 'not_run')}\n"
        f"- review_status: {review_status}\n- review_score_informational: {state.get('review_score', 0.0)}\n"
        f"- privileged_actions: {state.get('privileged_actions', [])}\n"
        f"- gate2_status: {privileged_execution_status or 'not_required'}\n"
        f"- plan_status: {state.get('plan_status', 'unknown')} (mock={state.get('plan_mock', False)})\n"
        f"- laya_shadow: {state.get('laya_task_type')} / confidence={state.get('laya_confidence')}\n"
        f"- search_status: {state.get('search_status')} (mock={state.get('search_mock')})\n"
        f"- memory_retrieval_status: {state.get('memory_status')}\n"
        f"- instruction_files_used: {state.get('instruction_files_used', [])}\n"
        f"- git_available: {state.get('git_available')}\n"
        f"- scope_violations: {state.get('scope_violations', [])}\n"
        "\nTest status `not_run` is not PASS. Mock outputs are not live verification.\n"
        "Gate 2 authorization does not execute privileged actions in this MVP.\n"
    )
    out = {"status": status, "deliverable": summary,
           "events": [{"type": "delivered", "status": status, "files": files,
                       "test_status": tests.get("status", "not_run"), "review_status": review_status}]}
    proposal = _memory_proposal({**state, **out})
    out["memory_proposal"] = proposal
    out["events"].append({"type": "memory_proposal", **proposal})
    return out
