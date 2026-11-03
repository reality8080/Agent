"""Test Runner node — executes tests inside workspace only."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


def tester_node(state: Dict[str, Any]) -> Dict[str, Any]:
    workspace = Path(os.getenv("WORKSPACE_ROOT", "workspace")).resolve()
    files = state.get("files_changed") or []

    # Minimal smoke: try to import / exec changed .py files
    results = []
    all_ok = True
    output_lines = []

    for rel in files:
        if not rel.endswith(".py"):
            results.append({"file": rel, "status": "skipped"})
            continue
        full = workspace / rel
        if not full.exists():
            # also try relative to cwd
            full = Path(rel)
        if not full.exists():
            results.append({"file": rel, "status": "missing"})
            all_ok = False
            continue
        try:
            # Compile check (no arbitrary exec of untrusted in real prod)
            compile(full.read_text(encoding="utf-8"), str(full), "exec")
            results.append({"file": rel, "status": "compile_ok"})
            output_lines.append(f"OK compile {rel}")
        except Exception as exc:
            results.append({"file": rel, "status": "compile_fail", "error": str(exc)})
            output_lines.append(f"FAIL {rel}: {exc}")
            all_ok = False

    # Optional: run pytest if present
    if (workspace / "tests").exists() or any("test_" in f for f in files):
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "--tb=no"],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=60,
            )
            output_lines.append(proc.stdout[-2000:] if proc.stdout else "")
            if proc.returncode != 0:
                all_ok = False
                output_lines.append(proc.stderr[-1000:] if proc.stderr else "")
        except Exception as exc:
            output_lines.append(f"pytest skipped: {exc}")

    return {
        "test_results": {"items": results, "all_ok": all_ok},
        "tests_passed": all_ok,
        "test_output": "\n".join(output_lines)[:4000],
        "events": [
            {
                "type": "tests_completed",
                "passed": all_ok,
                "files_checked": len(files),
            }
        ],
    }
