"""Deterministic workspace-only Test Runner with explicit non-PASS states."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from agent_system.workspace import get_workspace_root, normalize_relative_path


def tester_node(state: Dict[str, Any]) -> Dict[str, Any]:
    workspace = get_workspace_root(state.get("workspace_root"))
    records: List[Dict[str, Any]] = []
    output: List[str] = []
    files = list(state.get("files_changed") or [])
    if not workspace.is_dir():
        report = {"status": "not_run", "reason": "workspace_missing", "items": [],
                  "passed": 0, "failed": 0, "skipped": 0, "not_run": 1}
        return {"test_results": report, "tests_passed": False, "test_output": "Workspace missing; tests not run.",
                "events": [{"type": "tests_completed", "status": "not_run", "passed": False}]}

    for path in files:
        try:
            rel = normalize_relative_path(path)
            target = workspace / Path(*rel.split("/"))
            if any(p.is_symlink() for p in [target, *target.parents] if p != workspace):
                raise ValueError("symlink path blocked")
            target.resolve(strict=False).relative_to(workspace)
        except (ValueError, OSError):
            records.append({"file": str(path), "status": "failed", "reason": "unsafe_path"})
            continue
        if target.suffix.lower() != ".py":
            records.append({"file": rel, "status": "skipped", "reason": "non_python_file"})
            continue
        if not target.is_file():
            records.append({"file": rel, "status": "failed", "reason": "missing_in_workspace"})
            continue
        try:
            compile(target.read_text(encoding="utf-8"), rel, "exec")
            records.append({"file": rel, "status": "passed", "check": "compile"})
            output.append(f"PASS compile {rel}")
        except Exception as exc:
            records.append({"file": rel, "status": "failed", "check": "compile", "error": str(exc)})
            output.append(f"FAIL compile {rel}: {type(exc).__name__}: {exc}")

    test_files = []
    for tests_dir in (workspace / "tests", workspace / "src" / "tests"):
        if any(parent.is_symlink() for parent in [tests_dir, *tests_dir.parents] if parent != workspace):
            records.append({"check": "pytest", "status": "failed",
                            "reason": "tests_directory_symlink_blocked",
                            "path": tests_dir.relative_to(workspace).as_posix()})
            continue
        if not tests_dir.is_dir():
            continue
        for candidate in sorted(tests_dir.rglob("test_*.py")):
            try:
                candidate.resolve(strict=True).relative_to(workspace)
            except (ValueError, OSError):
                records.append({"check": "pytest", "status": "failed", "reason": "test_path_escape"})
                continue
            if candidate.is_file() and not candidate.is_symlink() and all(
                not parent.is_symlink() for parent in candidate.parents if parent != workspace
            ):
                test_files.append(candidate)
    test_files = sorted(set(test_files))
    pytest_status = "not_run"
    pytest_exit = None
    pytest_summary = {"passed": 0, "failed": 0, "skipped": 0, "not_run": 0}
    if test_files:
        timeout = max(1, min(int(os.getenv("AGENT_TEST_TIMEOUT", "90")), 600))
        docker_mode = os.getenv("AGENT_TEST_DOCKER", "1").lower() in ("1", "true", "yes")
        cmd = [sys.executable, "-m", "pytest", "-q", "-ra", "-p", "no:cacheprovider", "--tb=short"]
        if docker_mode:
            image = os.getenv("AGENT_DOCKER_IMAGE", "agent-system-test:local")
            cmd = ["docker", "run", "--rm", "--network=none", "--cpus=1", "--memory=1g",
                   "--pids-limit=128", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                   "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
                   "--user", "10001:10001", "--mount",
                   f"type=bind,source={workspace},target=/workspace,readonly", "-w", "/workspace",
                   image, "python", "-m", "pytest", "-q", "-ra", "-p", "no:cacheprovider", "--tb=short"]
        try:
            env = os.environ.copy()
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            proc = subprocess.run(cmd, cwd=str(workspace), capture_output=True, text=True,
                                  timeout=timeout, env=env, check=False)
            pytest_exit = proc.returncode
            stdout, stderr = proc.stdout or "", proc.stderr or ""
            output.append(stdout[-5000:])
            if stderr:
                output.append(stderr[-2500:])
            summary_text = "\n".join((stdout + "\n" + stderr).splitlines()[-12:])
            for key in ("passed", "failed", "skipped"):
                match = re.search(rf"(\d+)\s+{key}\b", summary_text, flags=re.I)
                pytest_summary[key] = int(match.group(1)) if match else 0
            if proc.returncode == 5:
                pytest_status = "not_run"
            elif proc.returncode != 0:
                pytest_status = "failed"
                if pytest_summary["failed"] == 0:
                    pytest_summary["failed"] = 1
            elif pytest_summary["passed"] == 0 and pytest_summary["skipped"] > 0:
                pytest_status = "skipped"
            else:
                pytest_status = "passed"
            pytest_summary["not_run"] = max(
                0, len(test_files) - pytest_summary["passed"] - pytest_summary["failed"] - pytest_summary["skipped"]
            )
            if pytest_status == "not_run":
                pytest_summary["not_run"] = len(test_files)
            records.append({"check": "pytest", "status": pytest_status,
                            "exit_code": proc.returncode, "test_count": len(test_files),
                            "test_cases": dict(pytest_summary),
                            "execution": "docker" if docker_mode else "local"})
        except FileNotFoundError:
            pytest_status = "not_run" if docker_mode else "failed"
            pytest_summary["not_run"] = len(test_files)
            records.append({"check": "pytest", "status": pytest_status,
                            "reason": "docker_not_installed" if docker_mode else "python_or_pytest_not_found",
                            "test_count": len(test_files), "test_cases": dict(pytest_summary),
                            "execution": "docker" if docker_mode else "local"})
        except subprocess.TimeoutExpired:
            pytest_status = "failed"
            pytest_summary["not_run"] = len(test_files)
            records.append({"check": "pytest", "status": "failed", "reason": "timeout",
                            "timeout_sec": timeout, "test_count": len(test_files),
                            "test_cases": dict(pytest_summary),
                            "execution": "docker" if docker_mode else "local"})
    else:
        pytest_summary["not_run"] = 1
        records.append({"check": "pytest", "status": "not_run", "reason": "no_workspace_tests_found",
                        "test_cases": dict(pytest_summary)})

    counts = {key: sum(1 for row in records if row.get("status") == key)
              for key in ("passed", "failed", "skipped", "not_run")}
    checks_ok = counts["failed"] == 0 and pytest_status == "passed"
    report_status = "failed" if counts["failed"] else ("passed" if checks_ok else "not_run")
    report = {"status": report_status, "items": records, **counts,
              "pytest_exit_code": pytest_exit, "pytest_summary": dict(pytest_summary),
              "workspace": str(workspace)}
    return {"test_results": report, "tests_passed": checks_ok,
            "test_output": "\n".join(x for x in output if x)[:10000],
            "events": [{"type": "tests_completed", "status": report_status,
                        "passed": checks_ok, "exit_code": pytest_exit,
                        "passed_count": counts["passed"], "failed_count": counts["failed"],
                        "skipped_count": counts["skipped"] + pytest_summary["skipped"],
                        "not_run_count": counts["not_run"] + pytest_summary["not_run"]}]}
