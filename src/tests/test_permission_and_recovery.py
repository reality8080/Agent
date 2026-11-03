"""Permission + recovery tests for Agent System v0.5 integration."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure src on path
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

os.environ["LLM_MOCK"] = "1"
os.environ["LAYA_MOCK"] = "1"
os.environ["SEARCH_MOCK"] = "1"
os.environ["GATE1_AUTO_APPROVE"] = "1"
os.environ["MEMORYLAKE_MOCK"] = "1"


class TestApprovalBinding(unittest.TestCase):
    def test_token_binds_to_plan_scope_env(self):
        from agent_system.security.approval import (
            issue_approval_token,
            verify_approval,
        )

        tok = issue_approval_token("pv1", "sh1", "env-a")
        self.assertTrue(
            verify_approval(tok.token, "pv1", "sh1", "env-a", tok.issued_at)
        )
        # Wrong plan_version
        self.assertFalse(
            verify_approval(tok.token, "pv2", "sh1", "env-a", tok.issued_at)
        )
        # Wrong scope
        self.assertFalse(
            verify_approval(tok.token, "pv1", "shX", "env-a", tok.issued_at)
        )
        # Wrong env
        self.assertFalse(
            verify_approval(tok.token, "pv1", "sh1", "env-b", tok.issued_at)
        )


class TestToolExecutorPermission(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["WORKSPACE_ROOT"] = self.tmp

    def test_write_blocked_without_gate1(self):
        from agent_system.security.tool_executor import ToolExecutor, PermissionError

        ex = ToolExecutor(
            workspace_root=self.tmp,
            approved_scope_paths=["src/"],
            # no token
        )
        with self.assertRaises(PermissionError):
            ex.execute_write("write_file", "src/a.py", "x = 1\n")

    def test_write_blocked_outside_scope(self):
        from agent_system.security.approval import issue_approval_token
        from agent_system.security.tool_executor import ToolExecutor, PermissionError

        tok = issue_approval_token("pv", "sh", "env")
        ex = ToolExecutor(
            workspace_root=self.tmp,
            approved_scope_paths=["src/"],
            gate1_token=tok.token,
            plan_version="pv",
            scope_hash="sh",
            environment_id="env",
            gate1_issued_at=tok.issued_at,
        )
        with self.assertRaises(PermissionError):
            ex.execute_write("write_file", "secrets/key.pem", "secret")

    def test_write_allowed_inside_scope_after_gate1(self):
        from agent_system.security.approval import issue_approval_token
        from agent_system.security.tool_executor import ToolExecutor

        tok = issue_approval_token("pv", "sh", "env")
        ex = ToolExecutor(
            workspace_root=self.tmp,
            approved_scope_paths=["src/"],
            gate1_token=tok.token,
            plan_version="pv",
            scope_hash="sh",
            environment_id="env",
            gate1_issued_at=tok.issued_at,
        )
        result = ex.execute_write("write_file", "src/ok.py", "print(1)\n")
        self.assertTrue(result["ok"])
        self.assertTrue((Path(self.tmp) / "src" / "ok.py").exists())


class TestEventLedgerIdempotency(unittest.TestCase):
    def test_idempotent_append(self):
        from agent_system.storage.event_ledger import EventLedger

        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "ledger.db")
            led = EventLedger(db_path=db)
            eid1 = led.append("test_event", action_id="a1", payload={"n": 1})
            eid2 = led.append("test_event", action_id="a1", payload={"n": 2})
            self.assertEqual(eid1, eid2)
            rows = led.get_by_action("a1")
            self.assertEqual(len(rows), 1)


class TestWorkflowSmoke(unittest.TestCase):
    def test_end_to_end_mock(self):
        from agent_system.graph import run_agent

        with tempfile.TemporaryDirectory() as td:
            os.environ["WORKSPACE_ROOT"] = td
            os.environ["CHECKPOINTER_DB"] = str(Path(td) / "cp.db")
            os.environ["EVENT_LEDGER_DB"] = str(Path(td) / "el.db")

            result = run_agent(
                "Write a tiny greeting module under workspace and verify it compiles."
            )
            self.assertIn(result.get("status"), ("completed", "failed", "waiting_approval"))
            # Laya must have written shadow fields
            self.assertTrue(result.get("laya_shadow"))
            self.assertIn("laya_task_type", result)
            # Search flag present
            self.assertIn("search_mock", result)
            # If completed, we should have files or a deliverable
            if result.get("status") == "completed":
                self.assertTrue(result.get("deliverable"))


if __name__ == "__main__":
    unittest.main()
