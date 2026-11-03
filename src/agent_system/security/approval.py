"""Approval token binding: plan_version + scope_hash + environment_id.

Gate 1 must present a valid token before any write tool is unlocked.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Secret used only for HMAC of approval tokens. Never log it.
_APPROVAL_SECRET = os.getenv("APPROVAL_HMAC_SECRET", "dev-only-change-me-in-prod")


@dataclass
class ApprovalToken:
    plan_version: str
    scope_hash: str
    environment_id: str
    issued_at: float
    expires_at: float
    token: str

    def is_valid(self, max_age_sec: int = 3600) -> bool:
        now = time.time()
        if now > self.expires_at:
            return False
        if now - self.issued_at > max_age_sec:
            return False
        expected = _compute_token(
            self.plan_version, self.scope_hash, self.environment_id, self.issued_at
        )
        return hmac.compare_digest(self.token, expected)


def _compute_token(
    plan_version: str, scope_hash: str, environment_id: str, issued_at: float
) -> str:
    msg = f"{plan_version}|{scope_hash}|{environment_id}|{issued_at:.6f}"
    return hmac.new(
        _APPROVAL_SECRET.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def issue_approval_token(
    plan_version: str,
    scope_hash: str,
    environment_id: str,
    ttl_sec: int = 3600,
) -> ApprovalToken:
    issued_at = time.time()
    token = _compute_token(plan_version, scope_hash, environment_id, issued_at)
    return ApprovalToken(
        plan_version=plan_version,
        scope_hash=scope_hash,
        environment_id=environment_id,
        issued_at=issued_at,
        expires_at=issued_at + ttl_sec,
        token=token,
    )


def verify_approval(
    token_str: str,
    plan_version: str,
    scope_hash: str,
    environment_id: str,
    issued_at: float,
    max_age_sec: int = 3600,
) -> bool:
    """Verify a token string against expected binding fields."""
    expected = _compute_token(plan_version, scope_hash, environment_id, issued_at)
    if not hmac.compare_digest(token_str, expected):
        return False
    now = time.time()
    if now - issued_at > max_age_sec:
        return False
    return True


def compute_scope_hash(paths: list[str], workspace_root: str = "") -> str:
    """Deterministic hash of approved write scope."""
    normalized = sorted(
        os.path.normpath(os.path.join(workspace_root, p)).replace("\\", "/")
        for p in paths
    )
    blob = "\n".join(normalized).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]
