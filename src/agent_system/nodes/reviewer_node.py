"""Reviewer node — read-only assessment before Gate 2."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def reviewer_node(state: Dict[str, Any]) -> Dict[str, Any]:
    from adapters.llm_adapter import LLMAdapter, LLMMode

    plan = state.get("plan", "")
    files = state.get("files_changed") or []
    tests_passed = state.get("tests_passed", False)
    test_output = state.get("test_output", "")
    user_request = state.get("user_request", "")

    system = (
        "You are the Reviewer for Agent System v0.5. "
        "Assess whether the code changes satisfy the plan and user request. "
        "Reply with:\n"
        "SCORE: <0.0-1.0>\n"
        "SUMMARY: <2-4 sentences>\n"
        "ISSUES: <bullet list or none>\n"
    )

    from adapters.llm_adapter import ModelProvider
    prov_name = os.getenv("LLM_PROVIDER", "gemini").lower()
    try:
        prov = ModelProvider(prov_name)
    except ValueError:
        prov = ModelProvider.GEMINI
    adapter = LLMAdapter(
        default_provider=prov,
        mock=os.getenv("LLM_MOCK", "1").lower() in ("1", "true", "yes"),
    )

    try:
        resp = adapter.complete(
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": (
                        f"Request: {user_request}\n\nPlan:\n{plan[:1500]}\n\n"
                        f"Files changed: {files}\nTests passed: {tests_passed}\n"
                        f"Test output:\n{test_output[:1000]}"
                    ),
                },
            ],
            mode=LLMMode.PLAN,
        )
        text = resp.content or ""
    except Exception as exc:
        logger.warning("Reviewer LLM failed: %s", exc)
        text = (
            f"SCORE: {'0.85' if tests_passed else '0.4'}\n"
            "SUMMARY: Fallback review based on test results only.\n"
            "ISSUES: none\n"
        )

    score = 0.5
    for line in text.splitlines():
        if line.strip().upper().startswith("SCORE:"):
            try:
                score = float(line.split(":", 1)[1].strip().split()[0])
            except Exception:
                pass
            break
    else:
        # Mock LLM does not emit SCORE — derive from tests
        score = 0.85 if tests_passed else 0.35

    # Bound by tests
    if not tests_passed:
        score = min(score, 0.45)

    return {
        "review": text[:3000],
        "review_score": score,
        "events": [
            {
                "type": "review_completed",
                "score": score,
                "tests_passed": tests_passed,
            }
        ],
    }
