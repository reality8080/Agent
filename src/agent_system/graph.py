"""LangGraph is the single controller for the complete Agent System workflow."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from langgraph.graph import END, StateGraph
from langgraph.types import Command

_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agent_system.state import AgentState
from agent_system.nodes.deliver_node import deliver_node, receive_node
from agent_system.nodes.laya_node import laya_shadow_node
from agent_system.nodes.memory_node import memory_retrieval_node
from agent_system.nodes.search_node import search_node
from agent_system.nodes.planner_node import planner_node
from agent_system.nodes.gate_node import gate1_node, gate2_node
from agent_system.nodes.coder_node import coder_node
from agent_system.nodes.tester_node import tester_node
from agent_system.nodes.reviewer_node import reviewer_node
from agent_system.storage.checkpointer import get_checkpointer
from agent_system.storage.event_ledger import EventLedger

logger = logging.getLogger(__name__)


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def _route_after_gate1(state: AgentState) -> str:
    return "coder" if state.get("gate1_approved") else "deliver"


def _route_after_review(state: AgentState) -> str:
    return "gate2" if state.get("privileged_actions") else "deliver"


def build_graph(checkpointer=None):
    """Compile workflow with durable SQLite checkpoint + real interrupt support."""
    graph = StateGraph(AgentState)
    for name, node in (
        ("receive", receive_node),
        ("laya_shadow", laya_shadow_node),
        ("search", search_node),
        ("memory_retrieval", memory_retrieval_node),
        ("planner", planner_node),
        ("gate1", gate1_node),
        ("coder", coder_node),
        ("test_runner", tester_node),
        ("reviewer", reviewer_node),
        ("gate2", gate2_node),
        ("deliver", deliver_node),
    ):
        graph.add_node(name, node)

    graph.set_entry_point("receive")
    graph.add_edge("receive", "laya_shadow")
    graph.add_edge("laya_shadow", "search")
    graph.add_edge("search", "memory_retrieval")
    graph.add_edge("memory_retrieval", "planner")
    graph.add_edge("planner", "gate1")
    graph.add_conditional_edges("gate1", _route_after_gate1, {"coder": "coder", "deliver": "deliver"})
    graph.add_edge("coder", "test_runner")
    graph.add_edge("test_runner", "reviewer")
    graph.add_conditional_edges("reviewer", _route_after_review,
                                {"gate2": "gate2", "deliver": "deliver"})
    graph.add_edge("gate2", "deliver")
    graph.add_edge("deliver", END)

    return graph.compile(checkpointer=checkpointer or get_checkpointer())


def _config(thread_id: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if config:
        result = dict(config)
        configurable = dict(result.get("configurable") or {})
        configured_thread = configurable.get("thread_id")
        if configured_thread and configured_thread != thread_id:
            raise ValueError("configurable.thread_id must match the workflow thread_id")
        configurable["thread_id"] = thread_id
        result["configurable"] = configurable
        return result
    return {"configurable": {"thread_id": thread_id}}


def _record_state(ledger: EventLedger, state: Dict[str, Any], thread_id: str) -> None:
    task_id, run_id, trace_id = state.get("task_id"), state.get("run_id"), state.get("trace_id")
    for index, event in enumerate(state.get("events") or []):
        if not isinstance(event, dict) or not event.get("type"):
            continue
        event_type = str(event["type"])
        action_id = str(event.get("action_id") or f"{run_id or thread_id}:state:{index}:{event_type}")
        ledger.append(
            event_type=event_type,
            action_id=action_id,
            thread_id=thread_id,
            task_id=task_id,
            run_id=run_id,
            trace_id=trace_id,
            plan_version=state.get("plan_version"),
            scope_hash=state.get("scope_hash"),
            environment_id=state.get("environment_id"),
            actor=str(event.get("actor") or "orchestrator"),
            payload=event,
            exit_code=event.get("exit_code"),
            artifact_refs=event.get("artifact_refs") or event.get("files") or [],
            tool_result=event.get("tool_result") if isinstance(event.get("tool_result"), dict) else None,
        )


def run_agent(
    user_request: str,
    thread_id: Optional[str] = None,
    environment_id: Optional[str] = None,
    workspace_root: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Start a graph run. A Gate 1 interrupt returns a resumable thread_id."""
    _load_env()
    from agent_system.workspace import get_workspace_root

    user_request = str(user_request or "").strip()
    if not user_request:
        raise ValueError("user_request must not be empty")
    thread_id = thread_id or str(uuid.uuid4())
    task_id, run_id, trace_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    environment_id = environment_id or os.getenv("ENVIRONMENT_ID", "local-dev")
    workspace = get_workspace_root(workspace_root)
    run_config = _config(thread_id, config)
    graph = build_graph()
    # A thread id is a durable identity and must never be reused for a new run.
    existing = graph.get_state(run_config)
    if (getattr(existing, "values", None) or {}).get("task_id") or (getattr(existing, "next", None) or ()):
        raise ValueError(f"Thread {thread_id!r} already has checkpointed workflow state; use --resume")
    ledger = EventLedger()
    request_digest = hashlib.sha256(user_request.encode("utf-8")).hexdigest()
    ledger.append(
        "workflow_started", action_id=f"{run_id}:workflow_started",
        thread_id=thread_id, task_id=task_id, run_id=run_id, trace_id=trace_id,
        environment_id=environment_id, actor="orchestrator",
        payload={"request_sha256": request_digest, "request_chars": len(user_request)},
    )
    initial: AgentState = {
        "user_request": user_request,
        "task_id": task_id, "run_id": run_id, "trace_id": trace_id,
        "thread_id": thread_id,
        "environment_id": environment_id,
        "workspace_root": str(workspace),
        "events": [], "messages": [],
    }
    result = graph.invoke(initial, config=run_config)
    result = dict(result or {})
    result.setdefault("thread_id", thread_id)
    _record_state(ledger, result, thread_id)
    snapshot = graph.get_state(run_config)
    next_nodes = tuple(getattr(snapshot, "next", ()) or ())
    is_interrupted = "gate1" in next_nodes or "gate2" in next_nodes
    if is_interrupted:
        result["status"] = "waiting_approval"
        result["pending_node"] = list(snapshot.next)
    ledger.append(
        "workflow_paused" if is_interrupted else "workflow_finished",
        action_id=f"{thread_id}:workflow_state:{'paused' if is_interrupted else 'finished'}",
        thread_id=thread_id, task_id=result.get("task_id"), run_id=result.get("run_id"),
        trace_id=result.get("trace_id"), plan_version=result.get("plan_version"),
        scope_hash=result.get("scope_hash"), environment_id=environment_id,
        payload={"status": result.get("status"), "pending_node": result.get("pending_node"),
                 "files_changed": result.get("files_changed", [])},
    )
    return result


def get_workflow_state(thread_id: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Read persisted graph state, including the pending interrupt node."""
    _load_env()
    graph = build_graph()
    snapshot = graph.get_state(_config(thread_id, config))
    return {"values": dict(snapshot.values or {}), "next": list(snapshot.next or []),
            "tasks": [getattr(task, "name", str(task)) for task in (snapshot.tasks or [])]}


def resume_agent(
    thread_id: str,
    approval: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resume a paused LangGraph thread using the original durable checkpoint."""
    _load_env()
    graph = build_graph()
    run_config = _config(thread_id, config)
    snapshot = graph.get_state(run_config)
    next_nodes = tuple(snapshot.next or ())
    if not next_nodes or not any(node in next_nodes for node in ("gate1", "gate2")):
        raise ValueError(f"Thread {thread_id!r} has no pending approval interrupt")
    state = dict(snapshot.values or {})
    pending_gate = "gate1" if "gate1" in next_nodes else "gate2"
    expected = {
        "plan_version": state.get("plan_version"),
        "scope_hash": state.get("scope_hash"),
        "environment_id": state.get("environment_id"),
    }
    if not isinstance(approval, dict):
        raise ValueError("Approval payload must be an object")
    ledger = EventLedger()
    stale = any(approval.get(key) != value for key, value in expected.items())
    wrong_actions = pending_gate == "gate2" and approval.get("actions") != state.get("privileged_actions")
    if stale or wrong_actions:
        ledger.append(
            "approval_rejected_stale", action_id=f"{state.get('run_id')}:stale:{uuid.uuid4()}",
            thread_id=thread_id, task_id=state.get("task_id"), run_id=state.get("run_id"),
            trace_id=state.get("trace_id"), plan_version=state.get("plan_version"),
            scope_hash=state.get("scope_hash"), environment_id=state.get("environment_id"),
            actor=str(approval.get("approved_by") or "user"),
            payload={"gate": pending_gate, "reason": "stale_binding" if stale else "wrong_actions"},
        )
        raise ValueError("Approval is stale or bound to a different plan, scope, environment, or action list")


    decision_for_log = {k: v for k, v in approval.items() if k not in ("token", "secret", "api_key")}
    approval_fingerprint = hashlib.sha256(json.dumps(decision_for_log, sort_keys=True, default=str).encode()).hexdigest()[:20]
    ledger.append(
        "human_approval_submitted", action_id=f"{state.get('run_id')}:approval:{pending_gate}:{approval_fingerprint}",
        thread_id=thread_id, task_id=state.get("task_id"), run_id=state.get("run_id"),
        trace_id=state.get("trace_id"), plan_version=state.get("plan_version"),
        scope_hash=state.get("scope_hash"), environment_id=state.get("environment_id"),
        actor=str(approval.get("approved_by") or "user"), approval=decision_for_log,
        payload={"gate": pending_gate, "approve": bool(approval.get("approve"))},
    )
    result = dict(graph.invoke(Command(resume=approval), config=run_config) or {})
    _record_state(ledger, result, thread_id)
    snapshot = graph.get_state(run_config)
    next_nodes = tuple(snapshot.next or ())
    paused = any(node in next_nodes for node in ("gate1", "gate2"))
    if paused:
        result["status"] = "waiting_approval"
        result["pending_node"] = list(next_nodes)
    ledger.append(
        "workflow_paused" if paused else "workflow_finished",
        action_id=f"{state.get('run_id')}:workflow_state:{'paused' if paused else 'finished'}:{pending_gate}",
        thread_id=thread_id, task_id=state.get("task_id"), run_id=state.get("run_id"),
        trace_id=state.get("trace_id"), plan_version=state.get("plan_version"),
        scope_hash=state.get("scope_hash"), environment_id=state.get("environment_id"),
        payload={"status": result.get("status"), "pending_node": list(next_nodes),
                 "files_changed": result.get("files_changed", [])},
    )
    return result
