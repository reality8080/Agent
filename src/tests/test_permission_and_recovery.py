"""Security, idempotency, SQLite ledger and workflow-node tests (offline-safe)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

os.environ.setdefault("LLM_MOCK", "1")
os.environ.setdefault("LAYA_MOCK", "1")
os.environ.setdefault("SEARCH_MOCK", "1")
os.environ.setdefault("MEMORYLAKE_MOCK", "1")
os.environ.setdefault("APPROVAL_HMAC_SECRET", "local-test-secret-that-is-at-least-32-characters")


def _executor(tmp_path: Path, scopes=None, **kwargs):
    from agent_system.security.approval import compute_scope_hash, issue_approval_token
    from agent_system.security.tool_executor import ToolExecutor

    scopes = scopes or ["src"]
    env = "test-env"
    scope_hash = compute_scope_hash(scopes, str(tmp_path))
    token = issue_approval_token("plan-v1", scope_hash, env)
    return ToolExecutor(
        workspace_root=str(tmp_path), approved_scope_paths=scopes,
        gate1_token=token.token, plan_version="plan-v1", scope_hash=scope_hash,
        environment_id=env, gate1_issued_at=token.issued_at,
        ledger=kwargs.pop("ledger", None), **kwargs,
    )


def test_approval_token_binds_plan_scope_environment_and_workspace(monkeypatch, tmp_path):
    from agent_system.security.approval import compute_scope_hash, issue_approval_token, verify_approval

    monkeypatch.setenv("APPROVAL_HMAC_SECRET", "test-approval-secret-value-32chars-minimum")
    h1 = compute_scope_hash(["src/"], str(tmp_path))
    token = issue_approval_token("pv1", h1, "env-a")
    assert verify_approval(token.token, "pv1", h1, "env-a", token.issued_at)
    assert not verify_approval(token.token, "pv2", h1, "env-a", token.issued_at)
    assert not verify_approval(token.token, "pv1", h1, "env-b", token.issued_at)
    assert compute_scope_hash(["src"], str(tmp_path / "other")) != h1


def test_gate1_always_requires_a_human_decision_even_when_auto_flag_is_set(monkeypatch, tmp_path):
    import types
    from agent_system.nodes.gate_node import gate1_node
    from agent_system.security.approval import compute_scope_hash

    monkeypatch.setenv("GATE1_AUTO_APPROVE", "1")
    monkeypatch.setenv("APPROVAL_HMAC_SECRET", "test-approval-secret-value-32chars-minimum")
    monkeypatch.setenv("APP_ENV", "development")
    scope = ["src"]
    scope_hash = compute_scope_hash(scope, str(tmp_path))
    decision = {"approve": True, "plan_version": "pv", "scope_hash": scope_hash,
                "environment_id": "local-dev", "approved_by": "human"}
    package = types.ModuleType("langgraph")
    package.__path__ = []
    types_module = types.ModuleType("langgraph.types")
    types_module.interrupt = lambda payload: decision
    monkeypatch.setitem(sys.modules, "langgraph", package)
    monkeypatch.setitem(sys.modules, "langgraph.types", types_module)
    result = gate1_node({"plan_version": "pv", "scope_paths": scope,
                         "scope_hash": scope_hash, "environment_id": "local-dev",
                         "workspace_root": str(tmp_path)})
    assert result["gate1_approved"] is True
    assert result["gate1_approved_by"] == "human"
    assert result["gate1_approval_token"]


def test_gate1_rejects_mismatched_scope_hash_before_interrupt(tmp_path):
    from agent_system.nodes.gate_node import gate1_node
    result = gate1_node({"plan_version": "pv", "scope_paths": ["src"],
                         "scope_hash": "stale", "environment_id": "local-dev",
                         "workspace_root": str(tmp_path)})
    assert result["gate1_approved"] is False
    assert result["status"] == "failed"


def test_tool_write_denied_without_approval(tmp_path):
    from agent_system.security.tool_executor import ToolExecutor, PermissionError
    from agent_system.storage.event_ledger import EventLedger
    ex = ToolExecutor(workspace_root=str(tmp_path), approved_scope_paths=["src"],
                      ledger=EventLedger(str(tmp_path / "ledger.db")))
    with pytest.raises(PermissionError):
        ex.execute_write("write_file", "src/a.py", "x=1")


def test_tool_write_with_gate1_and_idempotent_retry(monkeypatch, tmp_path):
    from agent_system.storage.event_ledger import EventLedger

    db = tmp_path / "ledger.db"
    ledger = EventLedger(str(db))
    ex = _executor(tmp_path, ledger=ledger)
    first = ex.execute_write("write_file", "src/ok.py", "print(1)\n", action_id="stable-1")
    second = ex.execute_write("write_file", "src/ok.py", "print(1)\n", action_id="stable-1")
    assert first["ok"] is True
    assert second["already_completed"] is True
    assert (tmp_path / "src" / "ok.py").read_text() == "print(1)\n"
    with pytest.raises(Exception):
        ex.execute_write("write_file", "src/ok.py", "print(2)\n", action_id="stable-1")
    rows = ledger.get_by_action("stable-1")
    assert [r["event_type"] for r in rows] == ["tool_action_started", "tool_action_completed"]


def test_scope_traversal_absolute_symlink_and_existing_file_blocked(tmp_path):
    from agent_system.security.tool_executor import PermissionError

    from agent_system.storage.event_ledger import EventLedger
    ex = _executor(tmp_path, scopes=["."], ledger=EventLedger(str(tmp_path / "ledger.db")))
    with pytest.raises(PermissionError):
        ex.execute_write("write_file", "../outside.txt", "no")
    with pytest.raises(PermissionError):
        ex.execute_write("write_file", str(tmp_path / "absolute.txt"), "no")

    existing = tmp_path / "src" / "preexisting.py"
    existing.parent.mkdir()
    existing.write_text("user content")
    with pytest.raises(PermissionError):
        ex.execute_write("write_file", "src/preexisting.py", "overwrite")

    external = tmp_path.parent / f"outside-{tmp_path.name}"
    external.mkdir(exist_ok=True)
    try:
        (tmp_path / "link").symlink_to(external, target_is_directory=True)
        with pytest.raises(PermissionError):
            ex.execute_write("write_file", "link/escape.py", "no")
    finally:
        if (tmp_path / "link").is_symlink():
            (tmp_path / "link").unlink()
        try:
            external.rmdir()
        except OSError:
            pass


def test_mock_tool_calls_have_no_side_effects(tmp_path):
    from agent_system.storage.event_ledger import EventLedger
    ex = _executor(tmp_path, scopes=["."], dry_run=True, ledger=EventLedger(str(tmp_path / "ledger.db")))
    result = ex.execute_write("write_file", "new.py", "no side effects")
    assert result["simulated"] is True
    assert not (tmp_path / "new.py").exists()


def test_event_ledger_append_only_hash_chain_and_secret_redaction(tmp_path):
    from agent_system.storage.event_ledger import EventLedger
    ledger = EventLedger(str(tmp_path / "events.db"))
    eid1 = ledger.append("tool_action_started", "act-1", payload={"password": "s3cret", "ok": True})
    eid2 = ledger.append("tool_action_started", "act-1", payload={"password": "different"})
    assert eid1 == eid2
    rows = ledger.get_by_action("act-1")
    assert len(rows) == 1
    assert "s3cret" not in rows[0]["payload"]
    assert ledger.verify_integrity()["ok"] is True
    import sqlite3
    with sqlite3.connect(ledger.db_path) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE events SET payload='{}' WHERE event_id=?", (eid1,))


def test_git_snapshot_tracks_existing_modifications_and_clean_files(tmp_path):
    from agent_system.workspace import git_snapshot
    if not shutil_which("git"):
        pytest.skip("git executable unavailable")
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "src").mkdir()
    tracked = tmp_path / "src" / "tracked.py"
    tracked.write_text("v1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "src/tracked.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], check=True,
                   capture_output=True)
    tracked.write_text("user edit\n")
    (tmp_path / "src" / "untracked.py").write_text("user file\n")
    snap = git_snapshot(tmp_path)
    assert snap["available"] is True
    assert "src/tracked.py" in snap["files"]
    assert "src/untracked.py" in snap["files"]
    assert "src/tracked.py" in snap["tracked_files"]


def shutil_which(name: str):
    import shutil
    return shutil.which(name)


def test_laya_shadow_is_typed_but_does_not_route(monkeypatch):
    from agent_system.nodes.laya_node import laya_shadow_node
    monkeypatch.setenv("LAYA_MOCK", "1")
    result = laya_shadow_node({"user_request": "implement a module"})
    assert result["laya_shadow"] is True
    assert result["laya_task_type"] == "coding"
    assert "route" not in result and "gate1_approved" not in result


def test_search_marks_mock_and_reads_workspace_only(monkeypatch, tmp_path):
    from agent_system.nodes.search_node import search_node
    monkeypatch.setenv("SEARCH_MOCK", "1")
    (tmp_path / "hello.py").write_text("def hello(): return 'hello'\n")
    result = search_node({"user_request": "hello module", "workspace_root": str(tmp_path)})
    assert result["search_mock"] is True
    assert all(hit["evidence_valid"] is False for hit in result["search_results"])
    assert result["repository_files"] == ["hello.py"]


def test_planner_one_model_and_workspace_bound_scope(monkeypatch, tmp_path):
    from agent_system.nodes.planner_node import planner_node
    from agent_system.security.approval import compute_scope_hash
    monkeypatch.setenv("LLM_MOCK", "1")
    result = planner_node({"user_request": "Create hello.py", "workspace_root": str(tmp_path),
                           "repository_context": [{"path": "hello.py", "content": "existing"}]})
    assert result["main_model"] == result["plan_model"] or result["plan_model"] == "fallback"
    assert result["scope_hash"] == compute_scope_hash(result["scope_paths"], str(tmp_path))
    assert result["status"] == "waiting_approval"


def test_memorylake_proposal_stays_proposed_or_disputed(monkeypatch):
    from agent_system.nodes.deliver_node import _memory_proposal
    from agent_system.runtime import clear_runtime_caches
    from agent_system.storage.event_ledger import EventLedger
    monkeypatch.setenv("MEMORYLAKE_PROJECT_ID", "project-test")
    monkeypatch.setenv("MEMORYLAKE_MOCK", "1")
    clear_runtime_caches()
    result = _memory_proposal({"status": "completed_with_warnings", "task_id": "t1", "run_id": "r1",
                              "git_diff": "", "files_changed": [], "test_results": {"status": "not_run"},
                              "review_status": "not_verified"})
    assert result["status"] in ("proposed", "disputed")
    assert result["mock"] is True
    clear_runtime_caches()


def test_real_graph_pause_resume_is_available_when_dependencies_installed(monkeypatch, tmp_path):
    pytest.importorskip("langgraph")
    pytest.importorskip("langgraph.checkpoint.sqlite")
    from agent_system.graph import get_workflow_state, resume_agent, run_agent
    monkeypatch.setenv("LLM_MOCK", "1")
    monkeypatch.setenv("LAYA_MOCK", "1")
    monkeypatch.setenv("SEARCH_MOCK", "1")
    monkeypatch.setenv("GATE1_AUTO_APPROVE", "0")
    monkeypatch.setenv("APPROVAL_HMAC_SECRET", "test-approval-secret-value-32chars-minimum")
    monkeypatch.setenv("CHECKPOINTER_DB", str(tmp_path / "checkpoints.sqlite"))
    monkeypatch.setenv("EVENT_LEDGER_DB", str(tmp_path / "events.sqlite"))
    monkeypatch.setenv("AGENT_TEST_DOCKER", "1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = run_agent("Create hello.py under workspace", thread_id="restart-test",
                       workspace_root=str(workspace))
    assert result["status"] == "waiting_approval"
    pending = get_workflow_state("restart-test")
    assert "gate1" in pending["next"]
    state = pending["values"]
    resumed = resume_agent("restart-test", {
        "approve": True, "plan_version": state["plan_version"],
        "scope_hash": state["scope_hash"], "environment_id": state["environment_id"],
        "approved_by": "test-user",
    })
    assert resumed["status"] in ("dry_run", "completed_with_warnings", "completed")



def test_tool_read_is_limited_to_approved_scope(tmp_path):
    from agent_system.security.tool_executor import PermissionError
    ex = _executor(tmp_path, scopes=["src"])
    (tmp_path / "private.txt").write_text("private")
    with pytest.raises(PermissionError):
        ex.execute_read("read_file", "private.txt")


def test_instruction_loader_records_common_and_relevant_skill(tmp_path):
    from agent_system.nodes.coder_node import _load_instructions
    skill = tmp_path / "SKILL.md"
    skill.write_text("Use deterministic test fixtures.\n", encoding="utf-8")
    content, used = _load_instructions({"scope_paths": ["src"]}, tmp_path)
    assert any(Path(path).name == "AGENTS.md" for path in used)
    assert str(skill) in used
    assert "YAGNI" in content and "PROTECT" in content
    assert "deterministic test fixtures" in content


def test_memory_retrieval_is_project_scoped_and_mock_is_not_live(monkeypatch):
    from agent_system.nodes.memory_node import memory_retrieval_node
    from agent_system.runtime import clear_runtime_caches, get_memorylake_adapter
    from adapters.memorylake_adapter import MemoryStatus, MemoryType
    monkeypatch.setenv("MEMORYLAKE_PROJECT_ID", "project-a")
    monkeypatch.setenv("MEMORYLAKE_MOCK", "1")
    clear_runtime_caches()
    adapter = get_memorylake_adapter()
    adapter.propose_memory("valve defect threshold", MemoryType.FACT, project_id="project-a",
                           status=MemoryStatus.VERIFIED, created_by="user-owner",
                           source_ref="repo:docs/valve.md")
    adapter.propose_memory("valve defect proposed", MemoryType.REFLECTION, project_id="project-a",
                           status=MemoryStatus.PROPOSED, created_by="agent:test")
    adapter.propose_memory("valve defect other project", MemoryType.FACT, project_id="project-b",
                           status=MemoryStatus.VERIFIED, created_by="user-owner")
    result = memory_retrieval_node({"user_request": "valve defect"})
    assert result["memory_mock"] is True
    assert len(result["memory_results"]) == 1
    assert result["memory_results"][0]["project_id"] == "project-a"
    assert result["memory_results"][0]["status"] == "verified"
    clear_runtime_caches()


def test_reviewer_mock_never_returns_pass(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from agent_system.nodes.reviewer_node import reviewer_node
    class MockAdapter:
        mock = True
        def complete(self, **kwargs):
            return SimpleNamespace(content='{"verdict":"PASS","summary":"Looks good","findings":[],"score":1.0}', model="mock", tool_calls=[])
    monkeypatch.setattr("agent_system.nodes.reviewer_node.main_llm_runtime", lambda: (MockAdapter(), "mock"))
    from agent_system.security.approval import compute_scope_hash
    scope_hash = compute_scope_hash(["src"], str(tmp_path))
    instructions = "YAGNI REUSE BUILT-IN EXISTING DEPENDENCY MINIMAL CODE PROTECT"
    result = reviewer_node({
        "scope_paths": ["src"], "workspace_root": str(tmp_path), "scope_hash": scope_hash,
        "instruction_files_used": ["AGENTS.md"], "instruction_content": instructions,
        "test_results": {"status": "passed"}, "plan": "approved", "git_diff": "", "files_changed": [],
    })
    # Mock output is not parsed as a trustworthy score or review decision.
    assert result["review_score"] == 0.0
    assert result["review_status"] == "not_verified"


def test_receive_preserves_run_and_trace_ids(tmp_path):
    from agent_system.nodes.deliver_node import receive_node
    state = receive_node({"user_request": "inspect", "task_id": "task-1", "thread_id": "thread-1",
                          "run_id": "run-1", "trace_id": "trace-1", "workspace_root": str(tmp_path)})
    assert (state["task_id"], state["run_id"], state["trace_id"]) == ("task-1", "run-1", "trace-1")


def test_coder_auto_mock_from_missing_credentials_is_still_dry_run(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from agent_system.nodes.coder_node import coder_node
    from agent_system.security.approval import compute_scope_hash, issue_approval_token
    monkeypatch.delenv("LLM_MOCK", raising=False)
    monkeypatch.setenv("AGENT_DRY_RUN", "0")
    scope = ["src"]
    scope_hash = compute_scope_hash(scope, str(tmp_path))
    token = issue_approval_token("plan-v1", scope_hash, "local-dev")
    class AutoMockAdapter:
        mock = True
        def complete(self, **kwargs):
            return SimpleNamespace(content="FILE: src/created.py\n```python\nprint('mock')\n```", model="mock", tool_calls=[])
    monkeypatch.setattr("agent_system.nodes.coder_node.main_llm_runtime", lambda: (AutoMockAdapter(), "mock"))
    result = coder_node({
        "gate1_approved": True, "gate1_approval_token": token.token,
        "gate1_issued_at": token.issued_at, "plan_version": "plan-v1", "scope_hash": scope_hash,
        "scope_paths": scope, "environment_id": "local-dev", "workspace_root": str(tmp_path),
        "task_id": "task-mock", "run_id": "run-mock", "trace_id": "trace-mock",
        "user_request": "create src/created.py", "plan": "minimal", "problem_brief": "create file",
    })
    assert result["coding_mock"] is True
    assert result["proposed_files"] == ["src/created.py"]
    assert not (tmp_path / "src" / "created.py").exists()


def test_tester_finds_src_tests_and_never_falls_back_outside_workspace(monkeypatch, tmp_path):
    from agent_system.nodes.tester_node import tester_node
    tests = tmp_path / "src" / "tests"
    tests.mkdir(parents=True)
    (tests / "test_local.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_TEST_DOCKER", "1")
    monkeypatch.setenv("AGENT_TEST_TIMEOUT", "2")
    result = tester_node({"workspace_root": str(tmp_path), "files_changed": []})
    assert result["test_results"]["items"][0]["check"] == "pytest"
    assert result["test_results"]["items"][0]["test_count"] == 1
    assert result["test_results"]["status"] in ("not_run", "failed")
    assert result["test_results"]["status"] != "passed" or result["test_results"]["pytest_exit_code"] == 0


def test_tool_executor_rejects_concurrent_modification_of_clean_tracked_file(tmp_path):
    if not shutil_which("git"):
        pytest.skip("git executable unavailable")
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    source = tmp_path / "src" / "tracked.py"
    source.parent.mkdir()
    source.write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "src/tracked.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "baseline"], check=True,
                   capture_output=True)
    import hashlib
    baseline = hashlib.sha256(source.read_bytes()).hexdigest()
    ex = _executor(tmp_path, scopes=["src"], git_available=True,
                   tracked_paths=["src/tracked.py"],
                   expected_file_hashes={"src/tracked.py": baseline})
    source.write_text("concurrent user edit\n", encoding="utf-8")
    from agent_system.security.tool_executor import PermissionError
    with pytest.raises(PermissionError, match="changed after Coding preparation"):
        ex.execute_write("write_file", "src/tracked.py", "agent overwrite")
    assert source.read_text(encoding="utf-8") == "concurrent user edit\n"


def test_tester_reports_pytest_skips_separately(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from agent_system.nodes.tester_node import tester_node
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_skips.py").write_text("def test_x(): pass\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_TEST_DOCKER", "0")
    monkeypatch.setattr("agent_system.nodes.tester_node.subprocess.run",
                        lambda *a, **k: SimpleNamespace(returncode=0,
                            stdout="2 passed, 3 skipped in 0.03s\n", stderr=""))
    result = tester_node({"workspace_root": str(tmp_path), "files_changed": []})
    assert result["test_results"]["status"] == "passed"
    assert result["test_results"]["pytest_summary"] == {
        "passed": 2, "failed": 0, "skipped": 3, "not_run": 0}


def test_reviewer_requires_deterministic_evidence_even_for_live_json_pass(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from agent_system.nodes.reviewer_node import reviewer_node
    from agent_system.security.approval import compute_scope_hash
    class LiveAdapter:
        mock = False
        def complete(self, **kwargs):
            return SimpleNamespace(content='{"verdict":"PASS","summary":"Reviewed diff","findings":[],"score":1.0}',
                                   model="live-test", tool_calls=[])
    monkeypatch.setattr("agent_system.nodes.reviewer_node.main_llm_runtime", lambda: (LiveAdapter(), "live-test"))
    scope_hash = compute_scope_hash(["src"], str(tmp_path))
    instructions = "YAGNI REUSE BUILT-IN EXISTING DEPENDENCY MINIMAL CODE PROTECT"
    result = reviewer_node({"scope_paths": ["src"], "workspace_root": str(tmp_path),
                            "scope_hash": scope_hash, "instruction_files_used": ["AGENTS.md"],
                            "instruction_content": instructions, "test_results": {"status": "not_run"},
                            "plan": "approved", "git_diff": "", "files_changed": []})
    assert result["review_score"] == 1.0
    assert result["review_status"] == "not_verified"


def test_reviewer_high_severity_finding_blocks_live_pass(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from agent_system.nodes.reviewer_node import reviewer_node
    from agent_system.security.approval import compute_scope_hash
    class LiveAdapter:
        mock = False
        def complete(self, **kwargs):
            return SimpleNamespace(content='{"verdict":"PASS","summary":"Reviewed diff",'
                                   '"findings":[{"severity":"high","file":"src/a.py",'
                                   '"line":3,"reason":"unsafe path use"}],"score":1.0}',
                                   model="live-test", tool_calls=[])
    monkeypatch.setattr("agent_system.nodes.reviewer_node.main_llm_runtime", lambda: (LiveAdapter(), "live-test"))
    scope_hash = compute_scope_hash(["src"], str(tmp_path))
    instructions = "YAGNI REUSE BUILT-IN EXISTING DEPENDENCY MINIMAL CODE PROTECT"
    result = reviewer_node({"scope_paths": ["src"], "workspace_root": str(tmp_path),
                            "scope_hash": scope_hash, "instruction_files_used": ["AGENTS.md"],
                            "instruction_content": instructions, "test_results": {"status": "passed"},
                            "plan": "approved", "git_diff": "", "files_changed": []})
    assert result["review_status"] == "fail"
    assert result["review_approved"] is False


def test_coder_revalidates_gate1_before_calling_coding_model(monkeypatch, tmp_path):
    from agent_system.nodes.coder_node import coder_node
    from agent_system.security.approval import compute_scope_hash
    class NeverCalled:
        mock = True
        def complete(self, **kwargs):
            raise AssertionError("Coding model must not be called without a valid approval token")
    monkeypatch.setattr("agent_system.nodes.coder_node.main_llm_runtime", lambda: (NeverCalled(), "mock"))
    scope = ["src"]
    result = coder_node({
        "gate1_approved": True, "gate1_approval_token": "tampered",
        "gate1_issued_at": 1.0, "plan_version": "plan-v1",
        "scope_hash": compute_scope_hash(scope, str(tmp_path)), "scope_paths": scope,
        "environment_id": "local-dev", "workspace_root": str(tmp_path),
        "task_id": "task-invalid-approval", "user_request": "change src/a.py",
    })
    assert result["status"] == "failed"
    assert result["events"][0]["reason"] == "invalid_gate1_approval"


def test_live_memory_without_explicit_project_provenance_is_discarded(monkeypatch):
    from types import SimpleNamespace
    from adapters.memorylake_adapter import MemoryScope, MemoryStatus, MemoryType, SharedMemory, MemorySearchHit
    from agent_system.nodes.memory_node import memory_retrieval_node
    monkeypatch.setenv("MEMORYLAKE_PROJECT_ID", "project-a")
    valid = SharedMemory("valid", MemoryType.FACT, "valid project data", MemoryScope(project_id="project-a"),
                         MemoryStatus.VERIFIED, source_ref="repo:docs/a.md", raw={
                             "metadata": {"scope": {"project_id": "project-a"}, "source_ref": "repo:docs/a.md"}})
    unscoped = SharedMemory("unscoped", MemoryType.FACT, "unscoped response", MemoryScope(project_id="project-a"),
                            MemoryStatus.VERIFIED, raw={"metadata": {"source_ref": "unknown"}})
    class LiveAdapter:
        mock = False
        def search(self, query, **kwargs):
            if kwargs.get("status") == MemoryStatus.VERIFIED:
                return [MemorySearchHit(valid), MemorySearchHit(unscoped)]
            return []
        def detect_conflicts(self, **kwargs):
            return SimpleNamespace(conflicts=[], mock=False)
    monkeypatch.setattr("agent_system.runtime.get_memorylake_adapter", lambda: LiveAdapter())
    result = memory_retrieval_node({"user_request": "project data"})
    assert [item["memory_id"] for item in result["memory_results"]] == ["valid"]
