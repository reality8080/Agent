# MemoryLake OAuth 2.1 Patch — Agent System Task 06

## Files in this patch

- `src/adapters/memorylake_adapter.py` — replace custom Bearer JSON-RPC with MCP SDK v2 OAuth 2.1 + Streamable HTTP; persist OAuth tokens and client registration through OS keyring; remove false Live-ready status and tool-name fallback; discover actual schemas; fail closed if an add-memory schema cannot carry project scope.
- `src/adapters/requirements-adapters.txt` — declare `mcp>=2.3.0,<3` and `keyring>=25.0.0`.
- `src/agent_system/nodes/memory_node.py` — enforce VERIFIED/APPROVED/DISPUTED locally even if remote `search_memory` ignores a status filter.
- `src/tests/test_memorylake_adapter.py` — update health expectations and add discovery, schema, secure storage and callback tests.
- `src/tests/test_permission_and_recovery.py` — cover filtering of proposed memory from Planner context.
- `scripts/memorylake_mcp_probe.py` — read-only OAuth + MCP initialize + tools/list probe; it does not write memory.
- `docs/MEMORYLAKE_MCP_SETUP.md` — update OAuth setup instructions.
- `README.md` — update Live configuration.

## Apply safely

1. Extract this patch into a temporary folder first.
2. Copy/merge only the listed files into the existing `D:\Agent` repository, preserving `.env`, `.mcp.json`, `.venv`, SQLite databases and all other user data.
3. Do not replace the whole repository with this patch archive.
4. The adapter no longer uses `MEMORYLAKE_MCP_TOKEN` as MCP authentication. The existing variable is ignored for this endpoint; keep the Personal REST API Key separate if you use REST elsewhere.
5. Keep the existing `.mcp.json` for VS Code unchanged.

## Runtime configuration

Keep the endpoint shown in the user's MemoryLake MCP page:

`https://app.memorylake.ai/memorylake/mcp/v2`

Required for the probe:

- `MEMORYLAKE_MCP_URL` — the endpoint above
- `MEMORYLAKE_MOCK=0`
- `MEMORYLAKE_PROJECT_ID` — Agent's internal project scope (does not automatically equal the OAuth-selected MemoryLake Workspace ID)
- `MEMORYLAKE_OAUTH_CALLBACK_PORT=8765` (optional; use a stable free localhost port)

OAuth tokens and client registration information are stored through Windows Credential Manager. No token cache file is created in the repository.

## Tests run in the patch-preparation environment

- `python -m compileall -q src run_agent.py scripts` — exit 0
- `python -m pytest src/tests -q -rs` — 55 passed, 1 skipped
- `python -m pytest src/tests/test_memorylake_adapter.py -q` — 17 passed
- The single skip is the LangGraph pause/resume test because LangGraph is not installed in the patch-preparation container. The user's Windows environment previously showed LangGraph installed, so rerun the full suite there.
- A local loopback callback smoke test passed; it exercised callback parsing and state/issuer passthrough with a test authorization result, not a live MemoryLake OAuth flow.

## Not yet verified

- Live MemoryLake OAuth authorization from the user's Windows process
- Live MCP initialize/tools/list and server-provided tool schemas
- Live search_memory/add_memory/detect_conflicts payload compatibility
- Live project isolation and proposal retrieval

The probe is read-only. Do not test writes until the real `add_memory` schema is discovered and confirmed to support project metadata/scope. Use a dedicated QA memory boundary for any later write test.
