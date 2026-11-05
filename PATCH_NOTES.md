# Agent System v0.5 — MemoryLake live-contract correction

Build date: 2026-11-05

## Purpose

This is an overlay patch for the source snapshot already carrying the MemoryLake OAuth patch. It aligns the adapter with the authenticated MCP schema observed from the user's own endpoint on 2026-11-05.

## Changes

- Use the live tool names `inspect` and `search`; do not call the legacy `search_memory`, `add_memory`, or `detect_conflicts` MCP tools.
- Call `inspect` before `search` in the same MCP session and verify the configured project ID is in the returned boundary.
- Match the observed `search` arguments: `queries`, `project_ids`, `return_types=["fact"]`, and `top_n`.
- Require explicit project provenance on every fact used by the Agent. Facts without a project scope are discarded.
- Preserve missing status as `None`; only `VERIFIED`, `APPROVED`, and `DISPUTED` are eligible for Planner context. Unknown, proposed, and deprecated facts are not passed as verified context.
- Treat conflict detection as unavailable because the observed server toolset does not expose it.
- Block live `remember` and `forget`. Deliver records a local pending proposal as a `memory_proposal` Event Ledger event. There is no remote write during this patch.
- Separate OAuth authentication, MCP discovery, inspect, and read health states; discovery alone no longer reports retrieval readiness.
- Add a read-only `scripts/memorylake_read_probe.py` diagnostic that does not print fact content and never calls `remember` or `forget`.
- Extend contract, project-boundary, result-parsing, unknown-status, and local-proposal tests.

## Files included

- `README.md`
- `docs/MEMORYLAKE_MCP_SETUP.md`
- `scripts/memorylake_read_probe.py` (new)
- `src/adapters/memorylake_adapter.py`
- `src/agent_system/nodes/deliver_node.py`
- `src/agent_system/nodes/memory_node.py`
- `src/agent_system/state.py`
- `src/tests/test_memorylake_adapter.py`
- `src/tests/test_permission_and_recovery.py`
- `apply_memorylake_fix.ps1`
- `manifest.json`

## Safety

- `.env`, OAuth credentials, API keys, SQLite databases, checkpoints, and user data are not included.
- The apply script verifies every target file against the expected pre-patch SHA-256 before writing anything. If a target file differs, it aborts before modifying any file.
- Existing target files are backed up under `.backup_memorylake_contract_fix_<timestamp>` before overlay.
- No remote MemoryLake write/delete is performed by the patch or probe.

## Verification performed in the assistant's isolated working copy

- `python -m compileall -q src run_agent.py scripts` — PASS.
- `python -m pytest -q -rs` — **70 passed, 1 skipped in 0.30s**.
- The skipped test requires LangGraph, which is not installed in this isolated Python 3.13.5 environment. The user's Windows Python 3.11.9 environment must run the full suite after applying the overlay.

These results do not mean the patch has been applied to `D:\Agent`, and do not establish live MemoryLake search success. The user must apply the patch and run the read-only probe on the Windows host.
