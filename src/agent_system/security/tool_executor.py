"""Secure tool executor.

- Path must be inside approved workspace scope.
- Does NOT trust tool name or prompt alone.
- Write tools blocked unless Gate 1 token is present and valid.
- Every action carries action_id for idempotency / ledger.
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agent_system.security.approval import verify_approval, compute_scope_hash

logger = logging.getLogger(__name__)


class PermissionError(Exception):
    pass


class ToolExecutor:
    """Runtime guard for all tools that touch the filesystem or external systems."""

    def __init__(
        self,
        workspace_root: str,
        approved_scope_paths: Optional[List[str]] = None,
        gate1_token: Optional[str] = None,
        plan_version: Optional[str] = None,
        scope_hash: Optional[str] = None,
        environment_id: Optional[str] = None,
        gate1_issued_at: Optional[float] = None,
    ):
        self.workspace_root = Path(workspace_root).resolve()
        self.approved_scope_paths = approved_scope_paths or []
        self.gate1_token = gate1_token
        self.plan_version = plan_version
        self.scope_hash = scope_hash
        self.environment_id = environment_id
        self.gate1_issued_at = gate1_issued_at

    def _is_path_allowed(self, target: str) -> bool:
        """True only if target resolves inside workspace AND inside approved scope."""
        try:
            resolved = (self.workspace_root / target).resolve()
        except Exception:
            return False

        # Must stay under workspace root (prevents ../ escape)
        try:
            resolved.relative_to(self.workspace_root)
        except ValueError:
            return False

        if not self.approved_scope_paths:
            # No scope approved yet → deny writes
            return False

        for allowed in self.approved_scope_paths:
            allowed_resolved = (self.workspace_root / allowed).resolve()
            try:
                resolved.relative_to(allowed_resolved)
                return True
            except ValueError:
                # exact file match
                if resolved == allowed_resolved:
                    return True
        return False

    def _gate1_ok(self) -> bool:
        if not all(
            [
                self.gate1_token,
                self.plan_version,
                self.scope_hash,
                self.environment_id,
                self.gate1_issued_at is not None,
            ]
        ):
            return False
        return verify_approval(
            self.gate1_token,
            self.plan_version,
            self.scope_hash,
            self.environment_id,
            self.gate1_issued_at,
        )

    def execute_write(
        self,
        tool_name: str,
        path: str,
        content: Optional[str] = None,
        action_id: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Execute a write tool under full permission checks."""
        action_id = action_id or str(uuid.uuid4())

        if not self._gate1_ok():
            raise PermissionError(
                f"Write tool '{tool_name}' blocked: Gate 1 approval missing or invalid"
            )

        if not self._is_path_allowed(path):
            raise PermissionError(
                f"Path '{path}' outside approved scope {self.approved_scope_paths}"
            )

        # Real write would happen here. For MVP we simulate + return evidence.
        full = self.workspace_root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        if content is not None:
            full.write_text(content, encoding="utf-8")

        return {
            "ok": True,
            "action_id": action_id,
            "tool": tool_name,
            "path": str(path),
            "bytes_written": len(content.encode("utf-8")) if content else 0,
        }

    def execute_read(
        self,
        tool_name: str,
        path: str,
        action_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Read is always allowed inside workspace (Plan mode)."""
        action_id = action_id or str(uuid.uuid4())
        try:
            resolved = (self.workspace_root / path).resolve()
            resolved.relative_to(self.workspace_root)
        except Exception as exc:
            raise PermissionError(f"Read path not in workspace: {path}") from exc

        if not resolved.exists():
            return {"ok": False, "action_id": action_id, "error": "not_found"}

        content = resolved.read_text(encoding="utf-8", errors="replace")
        return {
            "ok": True,
            "action_id": action_id,
            "tool": tool_name,
            "path": str(path),
            "content": content[:50_000],  # cap
        }


def make_executor_from_state(state: Dict[str, Any], workspace_root: str) -> ToolExecutor:
    return ToolExecutor(
        workspace_root=workspace_root,
        approved_scope_paths=state.get("scope_paths") or [],
        gate1_token=state.get("gate1_approval_token"),
        plan_version=state.get("plan_version"),
        scope_hash=state.get("scope_hash"),
        environment_id=state.get("environment_id"),
        gate1_issued_at=state.get("gate1_issued_at"),
    )
