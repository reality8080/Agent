"""Read-only Reviewer; deterministic evidence dominates model score."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List

from agent_system.runtime import main_llm_runtime
from agent_system.security.approval import compute_scope_hash

logger = logging.getLogger(__name__)


def reviewer_node(state: Dict[str, Any]) -> Dict[str, Any]:
    from adapters.llm_adapter import LLMMode

    findings: List[Dict[str, Any]] = []
    if state.get("scope_violations"):
        findings.append({"severity": "critical", "kind": "scope", "paths": state["scope_violations"],
                         "evidence": "actual changed paths exceed approved scope"})
    if state.get("status") == "failed":
        findings.append({"severity": "high", "kind": "execution",
                         "evidence": state.get("error") or "coder/workflow status is failed"})
    try:
        current_scope_hash = compute_scope_hash(state.get("scope_paths") or [], state.get("workspace_root", ""))
        if current_scope_hash != state.get("scope_hash"):
            findings.append({"severity": "critical", "kind": "approval_binding",
                             "evidence": "approved plan/scope/workspace binding no longer matches"})
    except Exception as exc:
        findings.append({"severity": "critical", "kind": "approval_binding",
                         "evidence": f"scope verification failed: {type(exc).__name__}"})
    instruction_files = state.get("instruction_files_used") or []
    instruction_text = state.get("instruction_content") or ""
    if not any(Path(str(item)).name.lower() == "agents.md" for item in instruction_files):
        findings.append({"severity": "high", "kind": "instruction_compliance",
                         "evidence": "common AGENTS.md was not loaded by Coding preparation"})
    required_order = ("YAGNI", "REUSE", "BUILT-IN", "EXISTING DEPENDENCY", "MINIMAL CODE", "PROTECT")
    positions = [instruction_text.find(term) for term in required_order]
    if not instruction_text or any(pos < 0 for pos in positions) or positions != sorted(positions):
        findings.append({"severity": "high", "kind": "instruction_compliance",
                         "evidence": "Ponytail order missing or not followed in loaded instructions"})
    tests = state.get("test_results") or {}
    if tests.get("status") == "failed":
        findings.append({"severity": "high", "kind": "tests", "evidence": state.get("test_output", "")[:1500]})
    elif tests.get("status") != "passed":
        findings.append({"severity": "medium", "kind": "tests_not_verified",
                         "evidence": f"test status={tests.get('status', 'not_run')}"})

    system = (
        "You are a read-only code reviewer. Do not edit files, execute tools or grant permissions. "
        "Inspect the approved plan, actual diff, changed paths, test evidence, security boundaries, "
        "pre-existing user changes and Ponytail instruction files. Return ONLY one valid JSON object "
        'with keys: verdict (PASS|FAIL|NOT_VERIFIED), summary (non-empty string), findings (array; '
        'each item has severity critical|high|medium|low, file, line and reason), score (optional 0..1). '
        "Do not return PASS if evidence is incomplete or a high/critical finding exists. Score is "
        "informational only and never approves Gate 1 or Gate 2."
    )
    user = (
        f"Approved plan:\n{state.get('plan', '')[:5000]}\n\n"
        f"Actual Git diff:\n{state.get('git_diff', '')[:12000]}\n\n"
        f"Changed files:\n{state.get('files_changed', [])}\n"
        f"Pre-existing dirty files protected:\n{state.get('git_preexisting_files', [])}\n"
        f"Test evidence:\n{state.get('test_results', {})}\n{state.get('test_output', '')[:4000]}\n\n"
        f"Instruction files used:\n{state.get('instruction_files_used', [])}\n"
        f"Instructions:\n{state.get('instruction_content', '')[:8000]}\n"
        f"Deterministic findings already detected:\n{findings}"
    )
    model_review_live = False
    try:
        adapter, model = main_llm_runtime()
        response = adapter.complete(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            mode=LLMMode.PLAN, model=state.get("main_model") or model,
        )
        review_text = response.content or "No model review text returned."
        model_review_live = bool(response.content) and not bool(getattr(adapter, "mock", False))
    except Exception as exc:
        logger.warning("Reviewer LLM unavailable: %s", type(exc).__name__)
        review_text = f"AI review not_run ({type(exc).__name__}); deterministic checks only."
    structured_review = None
    structured_valid = False
    if model_review_live:
        try:
            candidate_text = review_text.strip()
            if candidate_text.startswith("```"):
                candidate_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate_text, flags=re.I)
            parsed = json.loads(candidate_text)
            if (isinstance(parsed, dict)
                    and parsed.get("verdict") in {"PASS", "FAIL", "NOT_VERIFIED"}
                    and isinstance(parsed.get("summary"), str) and parsed["summary"].strip()
                    and isinstance(parsed.get("findings"), list)):
                structured_review = parsed
                structured_valid = True
                for item in parsed["findings"]:
                    if not isinstance(item, dict) or item.get("severity") not in {"critical", "high", "medium", "low"}:
                        structured_valid = False
                        break
                    findings.append({"severity": item["severity"], "kind": "llm_review",
                                     "file": str(item.get("file") or ""),
                                     "line": item.get("line"),
                                     "evidence": str(item.get("reason") or "")[:1000]})
        except (json.JSONDecodeError, TypeError, ValueError):
            structured_valid = False
    if not model_review_live or not structured_valid:
        findings.append({"severity": "medium", "kind": "reviewer_not_verified",
                         "evidence": "review was mock/unavailable or did not satisfy the structured JSON contract"})

    score = 0.0
    if structured_valid and isinstance(structured_review.get("score"), (int, float)):
        score = max(0.0, min(1.0, float(structured_review["score"])))
    else:
        match = re.search(r"(?im)^\s*SCORE\s*:\s*(0(?:\.\d+)?|1(?:\.0+)?)", review_text)
        if match:
            score = max(0.0, min(1.0, float(match.group(1))))
    # PASS requires structured live review AND independent deterministic evidence;
    # score alone is never consulted for the decision.
    has_blocker = any(f["severity"] in ("critical", "high") for f in findings)
    model_verdict = (structured_review or {}).get("verdict")
    if has_blocker or model_verdict == "FAIL":
        review_status = "fail"
    elif (tests.get("status") != "passed" or not model_review_live
          or not structured_valid or model_verdict != "PASS"):
        review_status = "not_verified"
    elif state.get("scope_violations"):
        review_status = "fail"
    else:
        review_status = "pass"
    return {"review": review_text[:6000], "review_score": score,
            "review_findings": findings, "review_status": review_status,
            "review_approved": review_status == "pass",
            "events": [{"type": "review_completed", "status": review_status,
                        "score_informational": score, "deterministic_findings": len(findings),
                        "model_review_live": model_review_live, "structured_review_valid": structured_valid,
                        "model_verdict": model_verdict,
                        "tests_status": tests.get("status", "not_run")}]} 
