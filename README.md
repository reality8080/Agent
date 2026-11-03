# Agent System v0.5 — LangGraph Integration

Integrated workflow that reuses existing adapters (LLM / Search / MemoryLake / Laya Shadow)
under a single LangGraph controller.

## Workflow

```
Receive → Laya Shadow → Search → Planner → Gate 1 → Coder → Test Runner → Reviewer → Gate 2 → Deliver
```

- **Laya**: Shadow mode only — writes recommendation + confidence into state, never routes or grants write permission.
- **Main LLM**: `LLMAdapter` (LiteLLM). Plan mode = read-only tools; Coding mode only after Gate 1.
- **Search**: `TavilySearchAdapter`. Mock results are flagged and never treated as real evidence.
- **MemoryLake**: adapter from Agent 05 (optional at runtime).
- **SQLite**: separate DBs for Checkpointer (pause/resume) and Event Ledger (audit).
- **Security**: Approval tokens bind `plan_version` + `scope_hash` + `environment_id`. Tool executor enforces workspace + approved scope paths.

## Quick start (mock mode)

```bash
cd Agent-main
export PYTHONPATH=src
export LLM_MOCK=1 LAYA_MOCK=1 SEARCH_MOCK=1 GATE1_AUTO_APPROVE=1
export WORKSPACE_ROOT=workspace
python -m agent_system.graph
```

Or programmatically:

```python
from agent_system.graph import run_agent
result = run_agent("Implement a greeting module under workspace/")
print(result["status"], result.get("deliverable"))
```

## Configuration

Copy `.env.example` → `.env` and set keys as needed.  
Environment variables take precedence; `providers.yaml` is optional catalogue only.

| Variable | Purpose |
|----------|---------|
| `LLM_PROVIDER` | openai / anthropic / gemini / grok / moonshot |
| `LLM_MODEL_PLAN` / `LLM_MODEL_CODING` | LiteLLM model ids |
| `LLM_MOCK` | 1 = deterministic mock, no API calls |
| `TAVILY_API_KEY` / `SEARCH_MOCK` | Search |
| `GATE1_AUTO_APPROVE` | 1 = auto-issue approval token (dev) |
| `APPROVAL_HMAC_SECRET` | HMAC secret for Gate 1 tokens |
| `CHECKPOINTER_DB` / `EVENT_LEDGER_DB` | Separate SQLite files |
| `WORKSPACE_ROOT` | Root of writable workspace |

## Tests

```bash
export PYTHONPATH=src
python -m pytest tests/test_permission_and_recovery.py -v
# or
python tests/test_permission_and_recovery.py
```

Coverage:
- Approval token binding (plan_version / scope_hash / environment_id)
- Tool executor: write blocked without Gate 1; path outside scope denied
- Event Ledger idempotency on `(action_id, event_type)`
- End-to-end mock workflow smoke

## Package layout

```
src/
  adapters/          # LLMAdapter, TavilySearchAdapter, MemoryLake (reused)
  Laya/              # Shadow triage (reused)
  agent_system/      # NEW integration layer
    graph.py         # LangGraph controller
    state.py
    nodes/           # receive, laya, search, planner, gate, coder, tester, reviewer, deliver
    security/        # approval tokens + tool executor
    storage/         # SQLite checkpointer + event ledger
  config/providers.yaml
tests/
workspace/           # default execution root
```

## Notes / MVP limits

- No NCM, subagent, or remote SSH.
- Ponytail / AGENTS.md skills can be layered on Coding mode later.
- Laya live model requires `pip install laya`; mock is default.
- Checkpointer uses SQLite + in-process MemorySaver cache; Event Ledger is append-only and independent.
