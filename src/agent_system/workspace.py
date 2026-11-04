"""Workspace path and Git helpers shared by workflow nodes."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, Iterable, List, Optional, Set

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def get_workspace_root(value: Optional[str] = None) -> Path:
    raw = value if value is not None else os.getenv("WORKSPACE_ROOT", "workspace")
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()


def normalize_relative_path(value: str, *, allow_dot: bool = False, strip_workspace_prefix: bool = True) -> str:
    """Normalize a path relative to WORKSPACE_ROOT and reject traversal/absolute paths."""
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("Path must be a non-empty relative path")
    raw = value.strip().replace("\\", "/")
    win = PureWindowsPath(raw)
    posix = PurePosixPath(raw)
    if win.is_absolute() or win.drive or posix.is_absolute():
        raise ValueError(f"Absolute paths are not allowed: {value!r}")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError(f"Path traversal is not allowed: {value!r}")
    if strip_workspace_prefix and parts and parts[0].casefold() == "workspace":
        parts = parts[1:]
    normalized = "/".join(parts)
    if not normalized:
        if allow_dot:
            return "."
        raise ValueError("A file path is required")
    return normalized


def normalize_scope_paths(paths: Iterable[str]) -> List[str]:
    normalized = sorted({normalize_relative_path(str(p), allow_dot=True) for p in paths})
    if not normalized:
        raise ValueError("Approved scope cannot be empty")
    return normalized


def path_is_in_scope(path: str, scopes: Iterable[str]) -> bool:
    target = normalize_relative_path(path)
    for raw_scope in scopes:
        scope = normalize_relative_path(str(raw_scope), allow_dot=True)
        if scope == "." or target == scope or target.startswith(scope.rstrip("/") + "/"):
            return True
    return False


def _parse_git_status(output: str) -> Set[str]:
    changed: Set[str] = set()
    for line in output.splitlines():
        if len(line) < 4:
            continue
        item = line[3:].strip()
        if " -> " in item:
            item = item.split(" -> ", 1)[1]
        item = item.strip('"')
        if item:
            try:
                changed.add(normalize_relative_path(item, strip_workspace_prefix=False))
            except ValueError:
                continue
    return changed


def git_snapshot(workspace_root: Path) -> Dict[str, Any]:
    """Read-only Git snapshot. No checkout/reset/staging/commit operations."""
    root = Path(workspace_root).resolve()
    if not root.exists():
        return {"available": False, "status": "workspace_missing", "files": [], "diff": ""}
    try:
        check = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        git_root = Path(check.stdout.strip()).resolve()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--short", "--untracked-files=all", "--" , "."],
            capture_output=True, text=True, timeout=10, check=True,
        )
        diff = subprocess.run(
            ["git", "-C", str(root), "diff", "--no-ext-diff", "--binary", "HEAD", "--", "."],
            capture_output=True, text=True, timeout=15, check=False,
        )
        staged = subprocess.run(
            ["git", "-C", str(root), "diff", "--cached", "--no-ext-diff", "--binary", "--", "."],
            capture_output=True, text=True, timeout=15, check=False,
        )
        tracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--", "."],
            capture_output=True, text=True, timeout=10, check=False,
        )
        tracked_files = sorted({normalize_relative_path(x, strip_workspace_prefix=False)
                                for x in tracked.stdout.splitlines() if x.strip()})
        diff_text = (diff.stdout or "") + (staged.stdout or "")
        return {
            "available": True,
            "status": status.stdout[:20000],
            "files": sorted(_parse_git_status(status.stdout)),
            "tracked_files": tracked_files,
            "diff": diff_text[:200000],
            "git_root": str(git_root),
        }
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
        return {"available": False, "status": f"unavailable: {type(exc).__name__}",
                "files": [], "tracked_files": [], "diff": ""}


def validate_changed_scope(paths: Iterable[str], scopes: Iterable[str]) -> List[str]:
    violations = []
    for raw in paths:
        try:
            normalized = normalize_relative_path(str(raw))
            if not path_is_in_scope(normalized, scopes):
                violations.append(normalized)
        except ValueError:
            violations.append(str(raw))
    return sorted(set(violations))
