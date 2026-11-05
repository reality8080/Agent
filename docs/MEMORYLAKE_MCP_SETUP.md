# MemoryLake MCP OAuth 2.1 — Agent System v0.5

## 1. Connection model

This project uses the **MemoryLake Memory MCP** endpoint shown by the account's MCP setup page:

```text
https://app.memorylake.ai/memorylake/mcp/v2
```

The MCP page labels this integration **OAuth 2.1**. It is a different credential flow from the Personal API Key page:

| Credential | Intended surface | Used by this adapter |
|---|---|---|
| Personal API Key | MemoryLake REST API V3 | No |
| MCP OAuth authorization | MemoryLake MCP | Yes |

Do not put a Personal API Key in `MEMORYLAKE_MCP_TOKEN` and expect it to authenticate the OAuth-protected MCP endpoint. The adapter keeps the old constructor argument for compatibility, but it does not send that value as a Bearer token.

The MCP endpoint and authorization server perform OAuth metadata discovery, dynamic client registration (when advertised), authorization-code + PKCE, token exchange and refresh through the official MCP Python SDK. The user chooses Personal/Team, Workspace and Memory boundary in the MemoryLake consent screen.

## 2. Requirements

Install adapter dependencies in the project's virtual environment:

```powershell
python -m pip install -r .\src\adapters\requirements-adapters.txt
```

The OAuth client uses:
- `mcp>=2.3.0,<3` (MCP SDK v2 API)
- `keyring>=25.0.0` (Windows Credential Manager backend)
- the SDK-provided `httpx2` transport dependency

On Windows, verify the credential backend before authorization:

```powershell
python -c "import keyring; b=keyring.get_keyring(); print(type(b).__name__, b.priority)"
```

The backend priority must be greater than zero. The adapter refuses to store OAuth credentials through a plaintext/fail backend.

## 3. Environment

Add these non-secret settings to `.env`:

```dotenv
MEMORYLAKE_MCP_URL=https://app.memorylake.ai/memorylake/mcp/v2
MEMORYLAKE_PROJECT_ID=your-agent-project-scope
MEMORYLAKE_USER_ID=
MEMORYLAKE_MOCK=0
MEMORYLAKE_OAUTH_CALLBACK_PORT=8765
```

`MEMORYLAKE_PROJECT_ID` is the Agent System's internal project scope. The OAuth consent's Workspace/Memory boundary is the remote account boundary. Do not assume their identifiers are interchangeable.

Do not store OAuth access tokens or refresh tokens in `.env`. The adapter stores OAuth tokens and dynamic client registration data through the OS credential manager, namespaced by MCP endpoint. Do not commit or log credential values.

`MEMORYLAKE_MCP_TOKEN` is deprecated for this OAuth MCP endpoint and ignored by the adapter. Keep a Personal REST API Key separate if another REST integration needs it.

## 4. First authorization and read-only discovery

From the repository root, with `.venv` active:

```powershell
python .\scripts\memorylake_mcp_probe.py
```

On first use:
1. The adapter starts a loopback callback listener on `127.0.0.1:8765`.
2. Your default browser opens the MemoryLake OAuth consent page.
3. Choose the intended Personal/Team, Workspace and Memory boundary.
4. Authorize the Agent System client.
5. The SDK validates the callback state/issuer, exchanges the code and stores tokens/client information in Windows Credential Manager.
6. The probe initializes MCP and displays the actual discovered tool names and input schema fields. It does not create, update or delete memory.

If port 8765 is occupied, set `MEMORYLAKE_OAUTH_CALLBACK_PORT` to another free localhost port and retry. Keep the chosen port stable between runs. The credential namespace includes the redirect URI, so changing the port creates a separate OAuth client registration rather than reusing a mismatched callback.

The VS Code authorization is not automatically shared with the Python process. The Agent System performs its own OAuth authorization once and then reuses its securely stored credentials.

## 5. Observed live tool contract and Agent behavior

The user's authenticated endpoint discovery on 2026-11-05 returned these tools:

- `inspect` — no arguments; its description says to call it first in each conversation.
- `search` — required `queries`; optional `project_ids`, `return_types`, `top_n`.
- `list_files` — optional `name_filter`, `project_ids`, `cursor`.
- `inspect_file` — required `file_id`; optional `include_download_url`.
- `run_code` — required `code`; optional `executor_id`, `file_ids`, `language`.
- `remember` — required `facts`; optional `scope`. This writes durable facts immediately.
- `forget` — required `fact_ids`; optional `scope`. This deletes facts.

This endpoint-specific discovery takes precedence over older integration examples that show `search_memory`, `add_memory` and `detect_conflicts`.

Live retrieval behavior:

1. Discover the current tool schemas from the authenticated endpoint.
2. Call `inspect` before `search` in the same MCP session.
3. Confirm `MEMORYLAKE_PROJECT_ID` is present in the projects visible to `inspect`.
4. Call `search` with `queries=[query]`, `project_ids=[project_id]`, `return_types=["fact"]`, and `top_n` from 1 to 100.
5. Require explicit returned fact scope matching the configured project. Unscoped or mismatched facts are discarded.
6. Preserve missing remote status as `None`; do not infer `approved` or `verified` when the server did not return a status.

The current live tool schema has no `status` filter and no `detect_conflicts` method. Conflict checks are therefore reported as unavailable, not as “zero conflicts”.

**Remote writes are disabled.** `remember` is a direct durable write and is not a staging/proposal endpoint. `forget` is destructive. Agent Deliver creates a local pending proposal that is included in the append-only SQLite Event Ledger; it does not call either remote tool. A separate explicit user-approval workflow is required before any future live write integration.

Health reporting distinguishes OAuth authentication, MCP discovery, inspect and read verification. Successful `tools/list` alone is not retrieval readiness. `ready_for_writes` remains false.

## 6. Verification stages

Start with read-only discovery:

```powershell
python .\scripts\memorylake_mcp_probe.py
python .\scripts\memorylake_read_probe.py --query "Agent System project context"
```

The first command verifies OAuth and tool discovery only. The second is read-only: it checks the inspect boundary and runs one project-scoped fact search; it prints identifiers/scope and response shape, not fact content. It does not call `remember` or `forget`.

Then run local tests:

```powershell
python -m compileall -q .\src .\run_agent.py .\scripts
python -m pytest .\src\tests -q -rs
```

After discovery, run the read-only retrieval probe. There is currently no remote proposal/write test: `remember` writes durable facts immediately and is blocked by this integration. Local pending proposals are written only to the SQLite Event Ledger. Do not call `remember` or `forget` as part of acceptance testing.

Classify outcomes accurately:
- `PASS`: live MCP initialize/discovery or operation succeeded with captured output.
- `BLOCKED`: OAuth consent, credentials, endpoint access or required tool/schema is unavailable.
- `FAIL`: an executed operation returned an error or violated the contract.
- `NOT TESTED`: operation was not attempted.

Mock tests do not prove Live connectivity.

## 7. Known boundaries

- This adapter does not replace LangGraph Checkpointer or SQLite Event Ledger.
- This adapter does not automatically map a MemoryLake Workspace ID to Agent `project_id`.
- Live retrieval must preserve explicit project provenance; unscoped results must not enter Planner context.
- Live conflict detection is unavailable with the currently discovered toolset; the Agent reports this limitation explicitly.
- Remote `remember` and `forget` are blocked; live promotion is unavailable unless the server exposes a documented update/promote operation and a separate approval path is implemented.
- The adapter does not use the Personal REST API Key for MCP OAuth.
