"""Read-only Planner: Problem Brief, provenance-aware evidence and implementation plan."""
from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Dict, List

from agent_system.security.approval import compute_scope_hash
from agent_system.workspace import normalize_scope_paths

logger = logging.getLogger(__name__)


def _evidence_context(state: Dict[str, Any]) -> tuple[str, List[Dict[str, Any]]]:
    lines: List[str] = []
    evidence: List[Dict[str, Any]] = []
    for item in state.get("search_results") or []:
        if item.get("mock"):
            lines.append(f"[MOCK SEARCH — NOT EVIDENCE] {item.get('title', '')}\n"
                         f"URL: {item.get('url', '')}\nContent: {(item.get('content') or '')[:1800]}")
            continue
        if not item.get("evidence_valid"):
            continue
        record = {"kind": "web", "title": item.get("title", ""), "url": item.get("url", ""),
                  "content": (item.get("content") or "")[:1800], "status": "live"}
        evidence.append(record)
        lines.append(f"[LIVE WEB] {record['title']}\nURL: {record['url']}\nContent: {record['content']}")
    for item in state.get("repository_context") or []:
        record = {"kind": "repository", "path": item.get("path"),
                  "status": item.get("status", "local_workspace"),
                  "content": (item.get("content") or "")[:1800]}
        evidence.append(record)
        lines.append(f"[LOCAL REPOSITORY] {record['path']} ({record['status']})\n{record['content']}")
    for item in state.get("memory_results") or []:
        record = {"kind": "memory", **item, "retrieval_mock": bool(state.get("memory_mock"))}
        if not state.get("memory_mock"):
            evidence.append(record)
        source_status = "MOCK MEMORY" if state.get("memory_mock") else item.get("status", "unknown").upper()
        lines.append(f"[{source_status}] {item.get('memory_type')} / {item.get('memory_id')}\n"
                     f"Source: {item.get('source_ref')}\nEvidence: {item.get('evidence_ref')}\n"
                     f"Content: {(item.get('content') or '')[:1200]}")
    if state.get("search_mock"):
        lines.append("[MOCK SEARCH] Results are excluded from evidence; do not cite or rely on them.")
    if state.get("memory_mock"):
        lines.append("[MOCK MEMORY] Retrieved context is simulated and not a live project-memory verification.")
    if state.get("memory_conflicts"):
        lines.append("[MEMORY CONFLICTS] Preserve and disclose these conflicts; do not silently resolve:\n" +
                     repr(state["memory_conflicts"][:5]))
    return "\n\n".join(lines) or "No verified external/project memory evidence was retrieved.", evidence


def _parse_scope(plan_text: str) -> List[str]:
    match = re.search(r"(?:^|\n)\s*SCOPE\s*:\s*(.*?)(?=\n\s*(?:PRIVILEGED ACTIONS|RISKS?|ACCEPTANCE|TESTS?|ROLLBACK)\s*:|\Z)",
                      plan_text, flags=re.I | re.S)
    paths = []
    if match:
        for line in match.group(1).splitlines():
            candidate = line.strip().lstrip("-*• ").strip().strip("`\"'")
            if candidate and not candidate.startswith("("):
                try:
                    paths.append(candidate)
                except Exception:
                    continue
    return normalize_scope_paths(paths) if paths else []


def planner_node(state: Dict[str, Any]) -> Dict[str, Any]:
    from adapters.llm_adapter import LLMMode
    from agent_system.runtime import main_llm_runtime

    adapter, main_model = main_llm_runtime()
    plan_mock = bool(getattr(adapter, "mock", False))
    plan_status = "mock" if plan_mock else "live"
    request = state.get("user_request", "")
    evidence_context, evidence = _evidence_context(state)
    system = (
        "You are the read-only Planner for Agent System v0.5. One physical main model is reused "
        "for Plan and Coding under separate permissions. You have no write capability.\n"
        "Produce: PROBLEM BRIEF (goal, current state, constraints, inputs/outputs, unknowns), "
        "EVIDENCE (separate facts, assumptions and unknowns; cite exact source title/URL or "
        "MemoryLake memory_id/status/source_ref), IMPLEMENTATION PLAN (minimal ordered tasks, "
        "exact workspace-relative paths, risks, test commands, acceptance criteria, rollback, "
        "and a PRIVILEGED ACTIONS: section listing only actions such as destructive delete/overwrite, "
        "system install, push/merge, SSH write, deploy or production operation; write `none` otherwise).\n"
        "Never represent mock search/memory as real evidence. Never treat Laya suggestion as "
        "routing or approval. Output a SCOPE: section with one path per line, followed by RISKS:."
    )
    user_message = (
        f"User request:\n{request}\n\nLaya shadow suggestion (non-authoritative): "
        f"task={state.get('laya_task_type')} risk={state.get('laya_risk')} "
        f"route={state.get('laya_route')} confidence={state.get('laya_confidence')} "
        f"abstain={state.get('laya_abstain')}\n\n"
        f"Project evidence and provenance:\n{evidence_context}\n\n"
        "Inspect the available project context read-only. Do not change source."
    )
    try:
        response = adapter.complete(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user_message}],
            mode=LLMMode.PLAN, model=main_model,
        )
        plan_text, model_used = response.content or "", response.model
    except Exception as exc:
        logger.exception("Planner LLM failed")
        plan_text = (
            "PROBLEM BRIEF:\n- Goal: " + request[:300] + "\n- State: not fully inspected\n"
            "EVIDENCE:\n- No verified evidence available\n"
            "IMPLEMENTATION PLAN:\n1. Inspect the approved workspace\n2. Make only the requested minimal change\n"
            "3. Run available tests and report skipped checks\n"
            "RISKS:\n- Planner model unavailable; bounded scope must be inferred from the request or repo\n"
        )
        model_used = "fallback"
        plan_mock = True
        plan_status = "fallback"
    try:
        scope_paths = _parse_scope(plan_text)
        if not scope_paths:
            # Offline/mock model fallback: only honor a path explicitly named by the user;
            # otherwise use a single repository match. Never silently approve the whole tree.
            explicit = re.findall(r"(?i)(?:workspace[\\/])?[a-z0-9_.-]+(?:[\\/][a-z0-9_.-]+)*\.[a-z0-9]{1,8}", request)
            if explicit:
                scope_paths = normalize_scope_paths(explicit[:3])
            else:
                return {"status": "failed", "error": "Planner did not define a bounded SCOPE; approval gate was not opened",
                        "events": [{"type": "plan_rejected", "reason": "missing_bounded_scope"}]}
    except ValueError as exc:
        return {"status": "failed", "error": f"Planner produced unsafe scope: {exc}",
                "events": [{"type": "plan_rejected", "reason": "unsafe_scope"}]}

    plan_version = str(uuid.uuid4())
    scope_hash = compute_scope_hash(scope_paths, state.get("workspace_root", ""))
    privileged_match = re.search(r"(?:^|\n)\s*PRIVILEGED ACTIONS\s*:\s*(.*?)(?=\n\s*(?:SCOPE|RISKS?|ACCEPTANCE|TESTS?|ROLLBACK)\s*:|\Z)",
                                 plan_text, flags=re.I | re.S)
    privileged_actions = []
    if privileged_match:
        for line in privileged_match.group(1).splitlines():
            item = line.strip().lstrip("-*• ").strip()
            if item and item.lower() not in {"none", "no", "n/a", "not required"}:
                if re.search(r"\b(delete|remove|overwrite|destructive|install|push|merge|ssh|deploy|production|prod)\b", item, re.I):
                    privileged_actions.append({"action": item, "approval_required": True})
    brief_match = re.search(r"(?:PROBLEM BRIEF|PROBLEM STATEMENT)\s*:?\s*(.*?)(?=\n\s*(?:EVIDENCE|IMPLEMENTATION PLAN|PLAN|SCOPE)\s*:|\Z)",
                            plan_text, flags=re.I | re.S)
    problem_brief = brief_match.group(1).strip()[:5000] if brief_match else plan_text[:2500]
    steps = [{"index": i + 1, "text": line.strip()} for i, line in enumerate(plan_text.splitlines())
             if re.match(r"^\s*\d+[.)]\s+", line)]
    return {
        "problem_brief": problem_brief, "evidence": evidence, "plan": plan_text,
        "plan_version": plan_version, "plan_steps": steps, "privileged_actions": privileged_actions,
        "scope_paths": scope_paths,
        "scope_hash": scope_hash, "plan_model": model_used, "main_model": main_model,
        "plan_mock": plan_mock, "plan_status": plan_status,
        "status": "waiting_approval",
        "events": [{"type": "plan_created", "plan_version": plan_version,
                    "scope_hash": scope_hash, "scope_paths": scope_paths, "model": model_used,
                    "plan_status": plan_status, "evidence_count": len(evidence),
                    "privileged_actions": privileged_actions}],
    }
