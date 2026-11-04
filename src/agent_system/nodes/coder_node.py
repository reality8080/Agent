"""Coding node: reuse the same main LLM under CODING permission mode."""
from __future__ import annotations

import difflib
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List

from agent_system.runtime import main_llm_runtime
from agent_system.security.approval import compute_scope_hash
from agent_system.security.tool_executor import make_executor_from_state
from agent_system.workspace import PROJECT_ROOT, get_workspace_root, git_snapshot, normalize_relative_path, path_is_in_scope, validate_changed_scope

logger = logging.getLogger(__name__)


def _load_instructions(state: Dict[str, Any], workspace: Path) -> tuple[str, List[str]]:
    """Load common Ponytail rules and project-local instruction files safely."""
    candidates: List[Path] = []
    ignored = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", "site-packages"}
    workspace = workspace.resolve()
    project_root = PROJECT_ROOT.resolve()

    # The Agent System's root AGENTS.md is the common, mandatory policy.
    common = project_root / "AGENTS.md"
    if common.is_file() and not common.is_symlink():
        candidates.append(common)

    # Never import unrelated SKILL.md files from this Agent System repository
    # into an external workspace. Project-local guidance is loaded from the
    # actual selected workspace only. If the workspace is this repo, the two
    # roots are identical and the project instructions are naturally included.
    base = workspace
    if base != project_root and (base / "AGENTS.md").is_file():
        candidates.append(base / "AGENTS.md")
    if base != project_root and (base / "SKILL.md").is_file():
        candidates.append(base / "SKILL.md")

    scope_paths = state.get("scope_paths") or []
    for pattern in ("AGENTS.md", "SKILL.md"):
        for path in sorted(base.rglob(pattern)):
            try:
                rel = path.relative_to(base)
                if any(part in ignored for part in rel.parts):
                    continue
                # Reject symlink files and any symlink parent; instruction loading
                # must not become an out-of-workspace read primitive.
                if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != base):
                    continue
                path.resolve(strict=True).relative_to(base)
                if path.name.lower() == "agents.md":
                    folder = rel.parent.as_posix()
                    if folder not in (".", "") and scope_paths and not any(
                        str(scope).replace("\\", "/").startswith(folder + "/")
                        or folder.startswith(str(scope).replace("\\", "/").rstrip("/") + "/")
                        or str(scope).replace("\\", "/") == folder
                        for scope in scope_paths
                    ):
                        continue
                if path not in candidates and path.is_file():
                    candidates.append(path)
                if len(candidates) >= 40:
                    break
            except (OSError, ValueError):
                continue
        if len(candidates) >= 40:
            break

    used, blocks = [], []
    for path in candidates:
        try:
            if path.is_symlink():
                continue
            content = path.read_text(encoding="utf-8")[:20000]
            rel = path.relative_to(project_root).as_posix() if path.is_relative_to(project_root) else str(path)
            used.append(rel)
            blocks.append(f"--- {rel} ---\n{content}")
        except (OSError, UnicodeError):
            continue
    return "\n\n".join(blocks), used


def _baseline_file_hashes(workspace: Path, snapshot: Dict[str, Any], scopes: List[str]) -> Dict[str, Any]:
    """Hash clean tracked files before LLM generation to detect concurrent user edits."""
    hashes: Dict[str, Any] = {}
    for rel in snapshot.get("tracked_files") or []:
        try:
            normalized = normalize_relative_path(rel, strip_workspace_prefix=False)
            if not path_is_in_scope(normalized, scopes):
                continue
            target = workspace / Path(*normalized.split("/"))
            if target.is_symlink() or not target.is_file() or target.stat().st_size > 20_000_000:
                hashes[normalized] = None
                continue
            hashes[normalized] = hashlib.sha256(target.read_bytes()).hexdigest()
        except (OSError, ValueError):
            hashes[str(rel)] = None
    return hashes


def _parse_file_blocks(content: str) -> List[tuple[str, str]]:
    pattern = re.compile(r"(?im)^\s*FILE:\s*(.+?)\s*\n\s*```[^\n]*\n(.*?)\n\s*```", re.DOTALL)
    return [(match.group(1).strip(), match.group(2) + "\n") for match in pattern.finditer(content or "")]


def coder_node(state: Dict[str, Any]) -> Dict[str, Any]:
    if not state.get("gate1_approved"):
        return {"status": "failed", "error": "Coder blocked: Gate 1 not approved",
                "events": [{"type": "coder_blocked", "reason": "gate1_not_approved"}]}
    workspace = get_workspace_root(state.get("workspace_root"))
    if compute_scope_hash(state.get("scope_paths") or [], str(workspace)) != state.get("scope_hash"):
        return {"status": "failed", "error": "Approved scope changed after Gate 1",
                "events": [{"type": "coder_blocked", "reason": "scope_hash_mismatch"}]}

    workspace.mkdir(parents=True, exist_ok=True)
    from adapters.llm_adapter import LLMMode
    adapter, model = main_llm_runtime()
    # The existing adapter can auto-enable mock mode when credentials are absent;
    # inspect its actual mode rather than only trusting environment flags.
    dry_run = (bool(getattr(adapter, "mock", False))
               or os.getenv("AGENT_DRY_RUN", "0").lower() in ("1", "true", "yes")
               or os.getenv("LLM_MOCK", "").lower() in ("1", "true", "yes"))
    before = git_snapshot(workspace)
    protected = set(state.get("git_preexisting_files") or []) | set(before.get("files") or [])
    tracked = before.get("tracked_files") or state.get("git_tracked_files") or []
    file_hashes = _baseline_file_hashes(workspace, before, list(state.get("scope_paths") or [])) if before.get("available") else {}
    executor_state = {**state, "git_preexisting_files": sorted(protected),
                      "git_tracked_files": tracked, "git_file_hashes": file_hashes,
                      "git_available": bool(before.get("available"))}
    executor = make_executor_from_state(executor_state, str(workspace), dry_run=dry_run)
    # Revalidate the signed Gate 1 grant before contacting the Coding model, not
    # merely at the later filesystem write boundary.
    if not executor.approval_valid():
        return {"status": "failed", "error": "Coder blocked: Gate 1 approval is missing, stale or mismatched",
                "events": [{"type": "coder_blocked", "reason": "invalid_gate1_approval"}]}
    instruction_text, instruction_files = _load_instructions(state, workspace)

    scope = state.get("scope_paths") or []
    system = (
        "You are the Coder for Agent System v0.5. Apply only the approved minimal implementation plan.\n"
        "You may return full file contents only in this exact format:\n"
        "FILE: <workspace-relative-path>\n```python\n<full file content>\n```\n"
        "Do not use absolute paths, parent traversal, shell commands, hidden extra files, or paths outside scope.\n"
        "Follow all project instructions below in order: YAGNI → REUSE → BUILT-IN → EXISTING DEPENDENCY → MINIMAL CODE → PROTECT.\n"
        "PROJECT INSTRUCTIONS:\n" + (instruction_text or "No instruction files found; use the safe defaults.")
    )
    user = (f"Approved plan version: {state.get('plan_version')}\nApproved scope: {scope}\n"
            f"Problem brief:\n{state.get('problem_brief', '')}\n\nPlan:\n{state.get('plan', '')}\n\n"
            f"User request:\n{state.get('user_request', '')}\n\n"
            f"Pre-existing uncommitted paths (do not overwrite): {sorted(protected)}\n"
            f"Git available: {before.get('available')}\n"
            f"Return only minimal FILE blocks. If no safe code change is required, explain that no patch is needed.")
    try:
        response = adapter.complete(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            mode=LLMMode.CODING, model=state.get("main_model") or model,
        )
        content, model_used = response.content or "", response.model
        tool_calls_ignored = len(response.tool_calls or [])
    except Exception as exc:
        logger.exception("Coding LLM failed")
        return {"status": "failed", "error": f"Coding model failed: {type(exc).__name__}",
                "instruction_files_used": instruction_files,
                "events": [{"type": "coder_failed", "error_type": type(exc).__name__}]}

    blocks = _parse_file_blocks(content)
    dry_run = executor.dry_run
    written, patches, patch_diffs = [], [], []
    if dry_run:
        proposed = []
        for raw_path, body in blocks:
            try:
                proposed.append(normalize_relative_path(raw_path))
            except ValueError:
                proposed.append(raw_path)
        return {
            "coding_mock": True, "code_model": model_used, "files_changed": [],
            "proposed_files": proposed, "code_patches": [], "coder_notes": content[:2500],
            "instruction_files_used": instruction_files, "instruction_content": instruction_text,
            "git_available": bool(before.get("available")), "git_status_coder_start": before.get("status", ""),
            "git_preexisting_files": sorted(protected), "git_tracked_files": tracked,
            "git_file_hashes": file_hashes, "git_status_after": before.get("status", ""),
            "git_diff": before.get("diff", ""), "git_changed_files": [], "scope_violations": [],
            "status": "dry_run",
            "events": [{"type": "coder_dry_run", "proposed_files": proposed,
                        "tool_calls_ignored": tool_calls_ignored, "mock": True}],
        }

    for raw_path, body in blocks:
        try:
            rel = normalize_relative_path(raw_path)
            stable_id = hashlib.sha256(
                f"{state.get('task_id')}|{state.get('plan_version')}|{rel}".encode("utf-8")
            ).hexdigest()[:32]
            prior = executor.execute_read("read_file", rel, action_id=f"{stable_id}:read")
            before_text = prior.get("content", "") if prior.get("ok") else ""
            result = executor.execute_write("write_file", rel, body, action_id=stable_id)
            written.append(rel)
            patches.append({"path": rel, "result": result})
            patch_diffs.append("".join(difflib.unified_diff(
                before_text.splitlines(keepends=True), body.splitlines(keepends=True),
                fromfile=f"a/{rel}" if prior.get("ok") else "/dev/null",
                tofile=f"b/{rel}",
            )))
        except Exception as exc:
            logger.warning("Patch write blocked for %r: %s", raw_path, type(exc).__name__)
            after = git_snapshot(workspace)
            return {"status": "failed", "error": f"Patch write blocked: {type(exc).__name__}",
                    "code_patches": patches, "files_changed": written,
                    "git_available": bool(after.get("available")),
                    "git_status_after": after.get("status", ""), "git_diff": after.get("diff", ""),
                    "instruction_files_used": instruction_files,
                    "events": [{"type": "coder_write_blocked", "path": str(raw_path),
                                "error_type": type(exc).__name__}]}

    after = git_snapshot(workspace)
    post_files = set(after.get("files") or [])
    pre_files = set(protected)
    git_new = sorted(post_files - pre_files)
    changed = sorted(set(written) | set(git_new))
    violations = validate_changed_scope(changed, scope)
    actual_diff = (after.get("diff") or "")
    patch_diff = "\n".join(x for x in patch_diffs if x)
    if patch_diff:
        actual_diff = (actual_diff + "\n" + patch_diff)[:250000]
    return {
        "coding_mock": False, "code_model": model_used, "files_changed": changed,
        "code_patches": patches, "coder_notes": content[:2500],
        "instruction_files_used": instruction_files, "instruction_content": instruction_text,
        "git_available": bool(after.get("available")), "git_status_coder_start": before.get("status", ""),
        "git_preexisting_files": sorted(protected), "git_tracked_files": tracked,
        "git_file_hashes": file_hashes, "git_status_after": after.get("status", ""),
        "git_diff": actual_diff, "git_changed_files": git_new,
        "scope_violations": violations,
        "status": "failed" if violations else ("completed" if changed else "no_changes"),
        "events": [{"type": "coder_completed", "files": changed, "model": model_used,
                    "action_count": len(patches), "scope_violations": violations,
                    "tool_calls_ignored": tool_calls_ignored}],
    }
