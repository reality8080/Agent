# MemoryLake MCP Integration — Agent System v0.5 (Task 05)

## 1. Official surface (verified from public docs)

| Item | Value |
|------|--------|
| MCP endpoint | `https://mcp.memorylake.ai` |
| Transport | HTTP (Streamable HTTP / JSON-RPC style) |
| Auth | `Authorization: Bearer <TOKEN>` |
| Token type | **Project-scoped** preferred (dashboard → project → API Keys / MCP token) |
| Documented MCP methods | `resources/list`, `resources/read` |
| Documented tools | `search_memory`, `add_memory`, `detect_conflicts` |

Sources:
- https://www.memorylake.ai/en/integrations/mcp
- https://docs.memorylake.ai/features/memorylake/api-reference/overview
- https://docs.memorylake.ai/features/memorylake/api-reference/authentication

**Important:** Exact tool argument schemas are not fully published in the marketing page. The adapter uses a conservative payload and **must** call `discover_tools()` / `tools/list` on first live session. Do not assume extra tools (update, delete, promote) exist until discovery confirms them.

## 2. Create project + project-scoped token

1. Sign up / log in at https://app.memorylake.ai/
2. Create or open a **Workspace**, then a **Project** (this becomes the Spec `project_id` namespace).
3. In the project, open **API Keys** (or MCP / Endpoint section).
4. Create a key intended for this project only (project-scoped).
5. Copy the secret once (`sk_…` or MCP bearer). Store only in `.env`.
6. Optionally note the project id (`proj-…` or custom_id) → set `MEMORYLAKE_PROJECT_ID`.

CLI alternative (if installed):

```bash
# https://github.com/memorylake-ai/memorylake-cli
memorylake auth login
memorylake workspace use
memorylake project create --name "AgentSystem" --custom-id agent-system-mvp
# then create API key from dashboard or key commands
```

## 3. Environment configuration

Add to project `.env` (never commit):

```bash
# MemoryLake MCP
MEMORYLAKE_MCP_URL=https://mcp.memorylake.ai
MEMORYLAKE_MCP_TOKEN=          # project-scoped bearer; leave empty → mock
MEMORYLAKE_PROJECT_ID=         # e.g. proj-... or custom_id
MEMORYLAKE_USER_ID=            # optional actor id
MEMORYLAKE_MOCK=1              # force mock; auto-mock when token missing
```

Adapter also respects the same variables when constructed without explicit args.

## 4. Adapter location & usage

```
src/adapters/memorylake_adapter.py
src/tests/test_memorylake_adapter.py
```

```python
from adapters.memorylake_adapter import (
    MemoryLakeAdapter,
    MemoryType,
    MemoryStatus,
    create_memorylake_adapter,
)

adapter = create_memorylake_adapter(mock=True, project_id="proj-demo")

# Propose only — never auto-approved
mem = adapter.propose_memory(
    content="Use append-only SQLite Event Ledger for operational evidence.",
    memory_type=MemoryType.FACT,
    project_id="proj-demo",
    task_id="task-42",
    source_ref="spec:v0.5#observability",
    evidence_ref=["event:run-1"],
    created_by="agent:planner",
    confidence=0.9,
)
assert mem.status == MemoryStatus.PROPOSED

hits = adapter.search("Event Ledger", project_id="proj-demo")
report = adapter.detect_conflicts(query="ledger", project_id="proj-demo")
```

### Spec contract mapping

| Spec field | Adapter handling |
|------------|------------------|
| `memory_id` | Client UUID; also sent in metadata |
| `memory_type` | `background\|fact\|event\|dialogue\|reflection\|skill` |
| `scope.project_id` | **Required** on write; passed to tools when supported |
| `status` | Defaults to `proposed`; agents cannot self-set `approved`/`verified` |
| `source_ref` / `evidence_ref` | Stored in metadata blob |
| `version` / `supersedes` / `conflicts_with` | Client-side; re-propose with supersedes after review |
| Promotion | Explicit `promote()` only for `user*` / `owner*` / `orchestrator*` actors; **live promote tool not documented** → `NotImplementedError` with guidance |

### Conflict & promotion policy (Spec §8)

- `detect_conflicts` surfaces conflicts; adapter never overwrites.
- Proposed memories stay proposed until owner/orchestrator promotion.
- Live MCP public tools do **not** document an update/promote tool → promotion in live mode is currently **BLOCKED** at the adapter (re-propose + supersedes, or use REST API after discovery).

## 5. Tests

```bash
# Mock / unit (no credentials)
MEMORYLAKE_MOCK=1 python src/tests/test_memorylake_adapter.py
```

Expected: all unit tests pass; last line prints  
`LIVE test: BLOCKED (no MEMORYLAKE_MCP_TOKEN)`.

### LIVE checklist (when token available)

1. Set `MEMORYLAKE_MCP_TOKEN` + `MEMORYLAKE_PROJECT_ID`, `MEMORYLAKE_MOCK=0`.
2. Run `adapter.discover_tools()` and log the returned names/schemas.
3. Call `propose_memory` → `search` → `detect_conflicts` on a throwaway project.
4. Confirm project isolation (token A cannot read project B).
5. Record any schema drift vs this adapter and update `_to_add_payload` / parsers.

Until step 1–3 succeed, treat LIVE as **BLOCKED**. Do not mark Task 05 complete for live connectivity.

## 6. What this adapter does *not* do

- Does not replace LangGraph checkpointer.
- Does not replace SQLite Event Ledger (operational evidence stays in ledger).
- Does not build a Memory Gateway (token granularity not yet proven insufficient).
- Does not implement self-hosted MemoryLake server.
- Does not invent MCP tools beyond documented + discovered names.

## 7. Residual risks / open items

| Item | Status |
|------|--------|
| Exact `add_memory` / `search_memory` argument schema | Must re-discover on first live session |
| Live status update / promote tool | Not documented → NotImplementedError |
| Resource URI scheme stability | Opaque; treat as string |
| Multi-tenant isolation proof | Requires two project tokens in LIVE test |
| Rate limits / quotas | Unknown; handle RuntimeError from transport |

---

**Handoff status**

- Adapter: `src/adapters/memorylake_adapter.py`
- Env: documented above + `.env.example` keys
- Project/token guide: this file §2
- Unit/mock tests: pass without credentials
- LIVE test: **BLOCKED** until `MEMORYLAKE_MCP_TOKEN` is supplied
