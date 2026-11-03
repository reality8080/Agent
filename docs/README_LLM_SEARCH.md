# LLM Provider & Search Adapters — Agent System v0.5

Unified configuration for **Main LLM** (via LiteLLM) and **LangChain Tavily Search**.

## Layout

```
AgentSystem/
├── adapters/
│   ├── __init__.py
│   ├── llm_adapter.py      # LiteLLM unified adapter (Plan / Coding modes)
│   └── search_adapter.py   # TavilySearch adapter
├── tests/
│   ├── test_llm_adapter.py
│   └── test_search_adapter.py
├── .env.example
└── README_LLM_SEARCH.md
```

## Prerequisites

```bash
# From project root (D:\AgentSystem or equivalent)
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install litellm langchain-tavily langchain-core python-dotenv pydantic
```

Do **not** install provider-specific SDKs. LiteLLM routes all calls.

Do **not** stand up a LiteLLM Proxy/Gateway for the MVP.

## Environment variables

Copy the sample and fill only the keys you need:

```bash
cp .env.example .env
# edit .env — never commit real keys
```

| Variable | Purpose |
|----------|---------|
| `LLM_MOCK` | `1` = never call real LLM APIs (default while no keys) |
| `SEARCH_MOCK` | `1` = never call Tavily |
| `OPENAI_API_KEY` | OpenAI |
| `ANTHROPIC_API_KEY` | Claude |
| `GOOGLE_API_KEY` / `GEMINI_API_KEY` | Gemini |
| `XAI_API_KEY` | Grok (xAI) |
| `MOONSHOT_API_KEY` / `KIMI_API_KEY` | Moonshot / Kimi |
| `LLM_MODEL_PLAN` | Optional LiteLLM model id for Plan mode |
| `LLM_MODEL_CODING` | Optional LiteLLM model id for Coding mode |
| `TAVILY_API_KEY` | Tavily Search |
| `SEARCH_MAX_RESULTS` | Default 5 (Planner-friendly) |

API keys must live only in `.env` (or the process environment). They must **never** appear in source, logs, or Git.

## How to obtain API keys

1. **OpenAI** — https://platform.openai.com/api-keys  
2. **Anthropic (Claude)** — https://console.anthropic.com/  
3. **Google Gemini** — https://aistudio.google.com/apikey  
4. **xAI Grok** — https://console.x.ai/  
5. **Moonshot / Kimi** — https://platform.moonshot.cn/ (or regional console)  
6. **Tavily** — https://tavily.com/ → dashboard → API keys  

Create keys with the minimum scopes required. Rotate if accidentally exposed.

## Usage examples

### Plan mode (read-only)

```python
from adapters import LLMAdapter, LLMMode, ModelProvider

adapter = LLMAdapter(mock=True)  # or create_llm_adapter()
resp = adapter.complete(
    messages=[
        {"role": "system", "content": "You are the Planner. Output Problem Brief + Implementation Plan."},
        {"role": "user", "content": "Add structured logging to the worker."},
    ],
    mode=LLMMode.PLAN,
    provider=ModelProvider.ANTHROPIC,
)
print(resp.content)
# Write tools are automatically stripped in PLAN mode.
```

### Coding mode (only after Gate 1)

```python
# Caller MUST enforce Gate 1 (plan_version + scope + environment approved)
# before entering Coding mode. This adapter still refuses tools if the
# selected model fails the tool-calling probe.

resp = adapter.complete(
    messages=[...],
    mode=LLMMode.CODING,
    tools=[{
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Apply a unified diff",
            "parameters": {...},
        },
    }],
)
```

### Search (Layer 1 evidence)

```python
from adapters import TavilySearchAdapter

search = TavilySearchAdapter(mock=True)  # or create_search_adapter()
resp = search.search("LangGraph interrupt human-in-the-loop")
for hit in resp.results:
    print(hit.title, hit.url)
    print(hit.content[:300])
```

## Tests — MOCK PASS vs LIVE API PASS

```bash
# Pure mock (no keys required) — expect "MOCK PASS"
LLM_MOCK=1 SEARCH_MOCK=1 python tests/test_llm_adapter.py
LLM_MOCK=1 SEARCH_MOCK=1 python tests/test_search_adapter.py

# Live (requires keys + LLM_MOCK=0 / SEARCH_MOCK=0)
# Will print "LIVE API PASS: ..." or SKIP if keys missing
LLM_MOCK=0 python tests/test_llm_adapter.py
SEARCH_MOCK=0 python tests/test_search_adapter.py
```

Interpretation:

- **MOCK PASS** — adapters behave correctly offline; safe for CI and for developers without keys.
- **LIVE API PASS** — real provider responded; only meaningful after keys are configured locally.
- **SKIP** — intentional when keys are absent; not a failure.

## Design notes aligned with Spec v0.5

| Spec requirement | How it is met |
|------------------|---------------|
| LiteLLM as unified adapter | Single `LLMAdapter` class; no per-provider SDKs |
| Plan mode no write | `_filter_tools_for_mode` strips write-like tools |
| Coding only after Gate 1 | Caller’s responsibility; adapter still probes tool support |
| Tool-calling check before code execution | `supports_tool_calling()` + hard block in Coding mode |
| Tavily via `langchain_tavily` | Preferred import; structured `SearchResult` (url/title/content) |
| No keys in source / Git | `.env.example` only; runtime reads `os.environ` |
| Mock when no keys | Auto-mock factories |

## Security reminders

- Never log `os.environ` values that may contain secrets.
- Keep `.env` in `.gitignore`.
- Prefer short-lived keys and least privilege.
- Gate 1 (plan approval) and Gate 2 (privileged actions) remain outside this adapter layer; they belong to the LangGraph workflow controller.
