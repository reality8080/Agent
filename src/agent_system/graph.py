"""LangGraph controller — sole orchestrator for Agent System v0.5.

Workflow:
  Receive → Laya Shadow → Search → Planner → Gate 1 → Coder → Test Runner → Reviewer → Gate 2 → Deliver
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from langgraph.graph import StateGraph, END

# Make src importable
_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agent_system.state import AgentState
from agent_system.nodes.deliver_node import receive_node, deliver_node
from agent_system.nodes.laya_node import laya_shadow_node
from agent_system.nodes.search_node import search_node
from agent_system.nodes.planner_node import planner_node
from agent_system.nodes.gate_node import gate1_node, gate2_node
from agent_system.nodes.coder_node import coder_node
from agent_system.nodes.tester_node import tester_node
from agent_system.nodes.reviewer_node import reviewer_node
from agent_system.storage.checkpointer import get_checkpointer
from agent_system.storage.event_ledger import EventLedger

logger = logging.getLogger(__name__)


def _route_after_gate1(state: AgentState) -> str:
    if state.get("gate1_approved"):
        return "coder"
    return "end_waiting"


def _route_after_gate2(state: AgentState) -> str:
    if state.get("review_approved"):
        return "deliver"
    return "end_failed"


def build_graph(checkpointer=None):
    """Construct and compile the LangGraph workflow."""
    g = StateGraph(AgentState)

    g.add_node("receive", receive_node)
    g.add_node("laya", laya_shadow_node)
    g.add_node("search", search_node)
    g.add_node("planner", planner_node)
    g.add_node("gate1", gate1_node)
    g.add_node("coder", coder_node)
    g.add_node("tester", tester_node)
    g.add_node("reviewer", reviewer_node)
    g.add_node("gate2", gate2_node)
    g.add_node("deliver", deliver_node)

    g.set_entry_point("receive")
    g.add_edge("receive", "laya")
    g.add_edge("laya", "search")
    g.add_edge("search", "planner")
    g.add_edge("planner", "gate1")

    g.add_conditional_edges(
        "gate1",
        _route_after_gate1,
        {
            "coder": "coder",
            "end_waiting": END,
        },
    )

    g.add_edge("coder", "tester")
    g.add_edge("tester", "reviewer")
    g.add_edge("reviewer", "gate2")

    g.add_conditional_edges(
        "gate2",
        _route_after_gate2,
        {
            "deliver": "deliver",
            "end_failed": END,
        },
    )
    g.add_edge("deliver", END)

    if checkpointer is None:
        checkpointer = get_checkpointer()

    return g.compile(checkpointer=checkpointer)


def run_agent(
    user_request: str,
    thread_id: Optional[str] = None,
    environment_id: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Convenience runner with Event Ledger side-effects."""
    import uuid

    thread_id = thread_id or str(uuid.uuid4())[:12]
    environment_id = environment_id or os.getenv("ENVIRONMENT_ID", "local-dev")

    ledger = EventLedger()
    graph = build_graph()

    init: AgentState = {
        "user_request": user_request,
        "thread_id": thread_id,
        "environment_id": environment_id,
        "events": [],
        "messages": [],
    }

    run_config = config or {
        "configurable": {"thread_id": thread_id},
    }

    # Record start
    action_id = str(uuid.uuid4())
    ledger.append(
        event_type="workflow_start",
        action_id=action_id,
        thread_id=thread_id,
        environment_id=environment_id,
        payload={"request": user_request[:500]},
    )

    final_state = None
    for event in graph.stream(init, config=run_config, stream_mode="values"):
        final_state = event
        # Append notable events to ledger
        for ev in event.get("events") or []:
            if isinstance(ev, dict) and ev.get("type"):
                ledger.append(
                    event_type=ev["type"],
                    action_id=action_id + ":" + ev["type"],
                    thread_id=thread_id,
                    task_id=event.get("task_id"),
                    plan_version=event.get("plan_version"),
                    scope_hash=event.get("scope_hash"),
                    environment_id=environment_id,
                    payload=ev,
                )

    ledger.append(
        event_type="workflow_end",
        action_id=action_id + ":end",
        thread_id=thread_id,
        environment_id=environment_id,
        payload={
            "status": (final_state or {}).get("status"),
            "files": (final_state or {}).get("files_changed"),
        },
    )

    return final_state or {}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    os.environ.setdefault("LLM_MOCK", "1")
    os.environ.setdefault("LAYA_MOCK", "1")
    os.environ.setdefault("SEARCH_MOCK", "1")
    os.environ.setdefault("GATE1_AUTO_APPROVE", "1")
    os.environ.setdefault("WORKSPACE_ROOT", "workspace")

    result = run_agent(
        "Implement a small hello_agent.py that prints a greeting and unit-test it."
    )
    print("=== FINAL STATUS ===")
    print(result.get("status"))
    print(result.get("deliverable"))
    print("files:", result.get("files_changed"))
    print("laya:", result.get("laya_task_type"), result.get("laya_confidence"))
    print("search_mock:", result.get("search_mock"))
    print("review_score:", result.get("review_score"))
