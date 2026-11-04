#!/usr/bin/env python3
"""CLI for starting and resuming the Agent System LangGraph workflow."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent System v0.5 — LangGraph controller")
    parser.add_argument("request", nargs="?", help="Task request (omit when resuming)")
    parser.add_argument("--thread-id", help="Persisted LangGraph thread id")
    parser.add_argument("--env-id", help="Environment binding for approval")
    parser.add_argument("--workspace", help="Workspace root (absolute or relative to project root)")
    parser.add_argument("--resume", action="store_true", help="Resume a paused approval thread")
    parser.add_argument("--approve", action="store_true", help="Approve the pending gate")
    parser.add_argument("--reject", action="store_true", help="Reject the pending gate")
    parser.add_argument("--plan-version")
    parser.add_argument("--scope-hash")
    parser.add_argument("--approved-by", default="user")
    parser.add_argument("--reason", default="")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    if args.workspace:
        os.environ["WORKSPACE_ROOT"] = args.workspace

    from agent_system.graph import get_workflow_state, resume_agent, run_agent

    if args.resume:
        if not args.thread_id or args.approve == args.reject:
            parser.error("--resume requires --thread-id and exactly one of --approve/--reject")
        current = get_workflow_state(args.thread_id)
        state = current.get("values") or {}
        decision = {
            "approve": args.approve,
            "plan_version": args.plan_version or state.get("plan_version"),
            "scope_hash": args.scope_hash or state.get("scope_hash"),
            "environment_id": args.env_id or state.get("environment_id"),
            "approved_by": args.approved_by,
            "reason": args.reason,
        }
        if "gate2" in current.get("next", []):
            decision["actions"] = state.get("privileged_actions", [])
        result = resume_agent(args.thread_id, decision)
    else:
        if not args.request:
            parser.error("provide a request, or use --resume with --thread-id")
        result = run_agent(args.request, thread_id=args.thread_id,
                           environment_id=args.env_id, workspace_root=args.workspace)

    output = {
        "status": result.get("status"), "thread_id": result.get("thread_id"),
        "pending_node": result.get("pending_node"), "task_id": result.get("task_id"),
        "plan_version": result.get("plan_version"), "plan_status": result.get("plan_status"),
        "plan_mock": result.get("plan_mock"), "scope_paths": result.get("scope_paths"),
        "scope_hash": result.get("scope_hash"), "environment_id": result.get("environment_id"),
        "problem_brief": result.get("problem_brief"), "plan": result.get("plan"),
        "files_changed": result.get("files_changed"), "test_results": result.get("test_results"),
        "review_status": result.get("review_status"), "memory_proposal": result.get("memory_proposal"),
        "deliverable": result.get("deliverable"),
    }
    print(json.dumps(output, indent=2, ensure_ascii=False, default=str))
    return 0 if result.get("status") in ("completed", "completed_with_warnings", "dry_run", "waiting_approval") else 1


if __name__ == "__main__":
    raise SystemExit(main())
