"""Workspace-scoped write executor with approval, symlink and retry guards."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_system.security.approval import compute_scope_hash, verify_approval
from agent_system.storage.event_ledger import EventLedger
from agent_system.workspace import normalize_relative_path, normalize_scope_paths, path_is_in_scope

logger = logging.getLogger(__name__)


class PermissionError(Exception):
    pass


class ToolExecutor:
    def __init__(
        self, workspace_root: str, approved_scope_paths: Optional[List[str]] = None,
        gate1_token: Optional[str] = None, plan_version: Optional[str] = None,
        scope_hash: Optional[str] = None, environment_id: Optional[str] = None,
        gate1_issued_at: Optional[float] = None, *, task_id: str = "",
        thread_id: str = "", run_id: str = "", trace_id: str = "",
        protected_paths: Optional[List[str]] = None, tracked_paths: Optional[List[str]] = None,
        expected_file_hashes: Optional[Dict[str, Optional[str]]] = None,
        git_available: bool = False, dry_run: bool = False,
        ledger: Optional[EventLedger] = None,
    ):
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.approved_scope_paths = normalize_scope_paths(approved_scope_paths or []) if approved_scope_paths else []
        self.gate1_token, self.plan_version = gate1_token, plan_version
        self.scope_hash, self.environment_id = scope_hash, environment_id
        self.gate1_issued_at = gate1_issued_at
        self.task_id, self.thread_id, self.run_id, self.trace_id = task_id, thread_id, run_id, trace_id
        self.protected_paths = {normalize_relative_path(p, strip_workspace_prefix=False) for p in (protected_paths or [])}
        self.tracked_paths = {normalize_relative_path(p, strip_workspace_prefix=False) for p in (tracked_paths or [])}
        self.expected_file_hashes = {
            normalize_relative_path(p, strip_workspace_prefix=False): digest
            for p, digest in (expected_file_hashes or {}).items()
        }
        self.git_available = bool(git_available)
        self.dry_run = bool(dry_run)
        self.ledger = ledger or EventLedger()

    def approval_valid(self) -> bool:
        """Public read-only Gate 1 validation for Coding preparation and writes."""
        if not all((self.gate1_token, self.plan_version, self.scope_hash, self.environment_id,
                    self.gate1_issued_at is not None, self.approved_scope_paths)):
            return False
        # Critical: recalculate from paths at time of write; never trust cached hash alone.
        current_hash = compute_scope_hash(self.approved_scope_paths, str(self.workspace_root))
        if not hmac_compare(current_hash, self.scope_hash):
            return False
        return verify_approval(
            self.gate1_token, self.plan_version, current_hash,
            self.environment_id, float(self.gate1_issued_at),
        )

    def _gate1_ok(self) -> bool:
        return self.approval_valid()

    def _safe_target(self, raw_path: str, *, allow_existing_for_reconcile: bool = False) -> tuple[str, Path]:
        try:
            rel = normalize_relative_path(raw_path)
        except ValueError as exc:
            raise PermissionError(str(exc)) from exc
        if not path_is_in_scope(rel, self.approved_scope_paths):
            raise PermissionError(f"Path {rel!r} is outside approved scope {self.approved_scope_paths}")
        if rel in self.protected_paths:
            raise PermissionError(f"Refusing to overwrite pre-existing user modification: {rel}")

        target = self.workspace_root / Path(*rel.split("/"))
        if target.exists() and not allow_existing_for_reconcile:
            if not self.git_available or rel not in self.tracked_paths:
                raise PermissionError(f"Refusing to overwrite an existing untracked/non-Git file: {rel}")
            expected_hash = self.expected_file_hashes.get(rel)
            if expected_hash is None or not target.is_file():
                raise PermissionError(f"No clean Git baseline is available for overwrite: {rel}")
            actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual_hash != expected_hash:
                raise PermissionError(f"File changed after Coding preparation; refusing overwrite: {rel}")
        # Reject every symlink component, even one that currently points inside workspace.
        current = self.workspace_root
        for part in rel.split("/"):
            current = current / part
            if current.is_symlink():
                raise PermissionError(f"Symlink paths are not writable: {rel}")
        resolved = target.resolve(strict=False)
        try:
            resolved.relative_to(self.workspace_root)
        except ValueError as exc:
            raise PermissionError(f"Path escapes workspace: {rel}") from exc
        # Re-check after resolving, including symlinked/non-existent parents.
        if not path_is_in_scope(resolved.relative_to(self.workspace_root).as_posix(), self.approved_scope_paths):
            raise PermissionError(f"Resolved path escapes approved scope: {rel}")
        return rel, resolved

    def execute_write(
        self, tool_name: str, path: str, content: Optional[str] = None,
        action_id: Optional[str] = None, **kwargs: Any,
    ) -> Dict[str, Any]:
        action_id = action_id or str(uuid.uuid4())
        if not self._gate1_ok():
            raise PermissionError(f"Write tool {tool_name!r} blocked: missing, stale or mismatched Gate 1 approval")
        try:
            rel = normalize_relative_path(path)
        except ValueError as exc:
            raise PermissionError(str(exc)) from exc
        if not path_is_in_scope(rel, self.approved_scope_paths):
            raise PermissionError(f"Path {rel!r} is outside approved scope {self.approved_scope_paths}")
        data = (content or "").encode("utf-8")
        requested_hash = hashlib.sha256(data).hexdigest()

        # Reconcile an earlier action before checking file existence: a completed
        # retry must return its result, never attempt the filesystem side effect again.
        if not self.dry_run:
            existing = self.ledger.get_by_action(action_id)
            completed = next((e for e in existing if e["event_type"] == "tool_action_completed"), None)
            if completed:
                payload = json.loads(completed.get("payload") or "{}")
                result = payload.get("tool_result") or {}
                if result.get("path") != rel or result.get("sha256") != requested_hash:
                    raise PermissionError(f"Action id {action_id} was already completed for different content/path")
                return {**result, "already_completed": True}
            started = next((e for e in existing if e["event_type"] == "tool_action_started"), None)
            if started:
                started_payload = json.loads(started.get("payload") or "{}")
                if started_payload.get("path") != rel or started_payload.get("requested_sha256") != requested_hash:
                    raise PermissionError(f"Action id {action_id} was already started for different content/path")
                _, retry_target = self._safe_target(rel, allow_existing_for_reconcile=True)
                if retry_target.is_file():
                    actual_hash = hashlib.sha256(retry_target.read_bytes()).hexdigest()
                    if actual_hash == requested_hash:
                        recovered = {"ok": True, "action_id": action_id, "tool": tool_name,
                                     "path": rel, "bytes_written": len(data), "sha256": actual_hash,
                                     "reconciled": True}
                        self.ledger.append(
                            "tool_action_completed", action_id, {"tool_result": recovered},
                            thread_id=self.thread_id, task_id=self.task_id, run_id=self.run_id,
                            trace_id=self.trace_id, plan_version=self.plan_version, scope_hash=self.scope_hash,
                            environment_id=self.environment_id, actor="coder", tool_result=recovered,
                            artifact_refs=[rel], exit_code=0,
                        )
                        return {**recovered, "already_completed": True}
                raise PermissionError(f"Action {action_id} is incomplete; no matching output exists. Manual reconciliation is required")

        rel, full = self._safe_target(rel)
        if self.dry_run:
            # Absolutely no file writes and no external tool execution in MOCK mode.
            return {"ok": False, "simulated": True, "action_id": action_id,
                    "tool": tool_name, "path": rel, "sha256": requested_hash}

        self.ledger.append(
            "tool_action_started", action_id,
            {"tool": tool_name, "path": rel, "requested_sha256": requested_hash},
            thread_id=self.thread_id, task_id=self.task_id, run_id=self.run_id,
            trace_id=self.trace_id, plan_version=self.plan_version, scope_hash=self.scope_hash,
            environment_id=self.environment_id, actor="coder",
        )
        try:
            # Create parents, then revalidate all path components to reduce symlink races.
            full.parent.mkdir(parents=True, exist_ok=True)
            rel, full = self._safe_target(rel)
            fd, temp_name = tempfile.mkstemp(prefix=".agent-write-", dir=str(full.parent))
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                if full.is_symlink():
                    raise PermissionError(f"Destination became a symlink: {rel}")
                os.replace(temp_name, full)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            result = {"ok": True, "action_id": action_id, "tool": tool_name,
                      "path": rel, "bytes_written": len(data), "sha256": requested_hash}
            self.ledger.append(
                "tool_action_completed", action_id, {"tool_result": result},
                thread_id=self.thread_id, task_id=self.task_id, run_id=self.run_id,
                trace_id=self.trace_id, plan_version=self.plan_version, scope_hash=self.scope_hash,
                environment_id=self.environment_id, actor="coder", tool_result=result,
                artifact_refs=[rel], exit_code=0,
            )
            return result
        except Exception as exc:
            self.ledger.append(
                "tool_action_failed", action_id, {"error_type": type(exc).__name__, "path": rel},
                thread_id=self.thread_id, task_id=self.task_id, run_id=self.run_id,
                trace_id=self.trace_id, plan_version=self.plan_version, scope_hash=self.scope_hash,
                environment_id=self.environment_id, actor="coder", exit_code=1,
            )
            raise

    def execute_read(self, tool_name: str, path: str, action_id: Optional[str] = None) -> Dict[str, Any]:
        try:
            rel = normalize_relative_path(path)
            target = self.workspace_root / Path(*rel.split("/"))
            if any(p.is_symlink() for p in [target, *target.parents] if p != self.workspace_root):
                raise PermissionError(f"Symlink read paths are not allowed: {rel}")
            resolved = target.resolve(strict=False)
            resolved.relative_to(self.workspace_root)
        except (ValueError, OSError) as exc:
            raise PermissionError(f"Read path not in workspace: {path}") from exc
        if not path_is_in_scope(rel, self.approved_scope_paths):
            raise PermissionError(f"Read path {rel!r} is outside approved scope {self.approved_scope_paths}")
        if not resolved.is_file():
            result = {"ok": False, "error": "not_found", "path": rel}
        else:
            content = resolved.read_text(encoding="utf-8", errors="replace")[:50000]
            result = {"ok": True, "tool": tool_name, "path": rel,
                      "content": content, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
        if self.ledger:
            read_action = action_id or hashlib.sha256(
                f"{self.run_id}|read|{rel}".encode("utf-8")
            ).hexdigest()[:32]
            self.ledger.append(
                "tool_read_completed", read_action,
                {"tool": tool_name, "path": rel, "ok": result["ok"],
                 "sha256": result.get("sha256"), "error": result.get("error")},
                thread_id=self.thread_id, task_id=self.task_id, run_id=self.run_id,
                trace_id=self.trace_id, plan_version=self.plan_version, scope_hash=self.scope_hash,
                environment_id=self.environment_id, actor="coder",
                artifact_refs=[rel], exit_code=0 if result["ok"] else 1,
            )
        return result


def hmac_compare(left: str, right: str) -> bool:
    import hmac
    return hmac.compare_digest(str(left), str(right))


def make_executor_from_state(state: Dict[str, Any], workspace_root: str, *, dry_run: bool = False) -> ToolExecutor:
    return ToolExecutor(
        workspace_root=workspace_root,
        approved_scope_paths=state.get("scope_paths") or [],
        gate1_token=state.get("gate1_approval_token"),
        plan_version=state.get("plan_version"),
        scope_hash=state.get("scope_hash"),
        environment_id=state.get("environment_id"),
        gate1_issued_at=state.get("gate1_issued_at"),
        task_id=state.get("task_id", ""), thread_id=state.get("thread_id", ""),
        run_id=state.get("run_id", ""), trace_id=state.get("trace_id", ""),
        protected_paths=state.get("git_preexisting_files") or [],
        tracked_paths=state.get("git_tracked_files") or [],
        expected_file_hashes=state.get("git_file_hashes") or {},
        git_available=bool(state.get("git_available")), dry_run=dry_run,
    )
