"""Approval tokens bound to immutable plan, path scope and environment."""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from agent_system.workspace import normalize_scope_paths


@dataclass(frozen=True)
class ApprovalToken:
    plan_version: str
    scope_hash: str
    environment_id: str
    issued_at: float
    expires_at: float
    token: str

    def is_valid(self, max_age_sec: int = 3600) -> bool:
        return verify_approval(
            self.token, self.plan_version, self.scope_hash, self.environment_id,
            self.issued_at, max_age_sec=max_age_sec,
        ) and time.time() <= self.expires_at


def _secret() -> bytes:
    value = os.getenv("APPROVAL_HMAC_SECRET", "")
    if len(value) < 32:
        raise RuntimeError("APPROVAL_HMAC_SECRET must contain at least 32 characters")
    return value.encode("utf-8")


def _compute_token(plan_version: str, scope_hash: str, environment_id: str, issued_at: float) -> str:
    msg = f"{plan_version}|{scope_hash}|{environment_id}|{issued_at:.6f}"
    return hmac.new(_secret(), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_approval_token(
    plan_version: str, scope_hash: str, environment_id: str, ttl_sec: int = 3600,
) -> ApprovalToken:
    issued_at = time.time()
    token = _compute_token(plan_version, scope_hash, environment_id, issued_at)
    return ApprovalToken(plan_version, scope_hash, environment_id, issued_at, issued_at + ttl_sec, token)


def verify_approval(
    token_str: str, plan_version: str, scope_hash: str, environment_id: str,
    issued_at: float, max_age_sec: int = 3600,
) -> bool:
    try:
        expected = _compute_token(plan_version, scope_hash, environment_id, issued_at)
    except RuntimeError:
        return False
    if not hmac.compare_digest(str(token_str or ""), expected):
        return False
    age = time.time() - float(issued_at)
    return 0 <= age <= max_age_sec


def compute_scope_hash(paths: list[str], workspace_root: str = "") -> str:
    """Bind normalized approved paths to the canonical workspace location."""
    normalized = normalize_scope_paths(paths)
    workspace = str(Path(workspace_root).expanduser().resolve()) if workspace_root else ""
    payload = "\n".join((workspace, *normalized))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
