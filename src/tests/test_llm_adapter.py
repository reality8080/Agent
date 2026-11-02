"""
Unit / integration tests for LLMAdapter.

Run with:
  LLM_MOCK=1 python src/tests/test_llm_adapter.py
  LLM_MOCK=0 python src/tests/test_llm_adapter.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_CANDIDATE_ROOTS = [
    _HERE.parent,                 # .../tests
    _HERE.parent.parent,          # .../src/tests -> repo root
    Path.cwd(),
    Path.cwd().parent,
]

# Prefer repo root that contains adapters/ or src/adapters/
ROOT = Path.cwd()
for _r in _CANDIDATE_ROOTS:
    if (_r / "adapters").is_dir() or (_r / "src" / "adapters").is_dir():
        ROOT = _r
        break

for _p in (ROOT, ROOT / "src"):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Explicitly load every plausible .env (do not print secret values)
_LOADED_ENV_FILES: list[str] = []
try:
    from dotenv import load_dotenv, dotenv_values

    for _base in list(dict.fromkeys(_CANDIDATE_ROOTS + [ROOT, Path.cwd()])):
        _env_path = _base / ".env"
        if _env_path.is_file():
            load_dotenv(_env_path, override=True)
            _LOADED_ENV_FILES.append(str(_env_path))
    load_dotenv(override=False)
except ImportError:
    dotenv_values = None  # type: ignore


from adapters.llm_adapter import (
    LLMAdapter,
    LLMMode,
    ModelProvider,
    create_llm_adapter,
)


def test_resolve_model_defaults():
    adapter = LLMAdapter(mock=True)
    assert "gpt" in adapter.resolve_model(ModelProvider.OPENAI).lower() or "openai" in adapter.resolve_model(ModelProvider.OPENAI)
    assert "claude" in adapter.resolve_model(ModelProvider.ANTHROPIC).lower()
    assert "gemini" in adapter.resolve_model(ModelProvider.GEMINI).lower()
    assert "grok" in adapter.resolve_model(ModelProvider.GROK).lower() or "xai" in adapter.resolve_model(ModelProvider.GROK)
    assert "moonshot" in adapter.resolve_model(ModelProvider.MOONSHOT).lower()


def test_plan_mode_strips_write_tools():
    adapter = LLMAdapter(mock=True)
    write_tools = [
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write content to a file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        },
    ]
    resp = adapter.complete(
        messages=[{"role": "user", "content": "Draft a plan for adding logging."}],
        mode=LLMMode.PLAN,
        tools=write_tools,
    )
    assert resp.mode == LLMMode.PLAN
    assert "[MOCK PLAN]" in resp.content
    assert resp.tool_calls == [] or all(tc["name"] != "write_file" for tc in resp.tool_calls)


def test_coding_mode_mock_emits_tool_call_when_tools_provided():
    adapter = LLMAdapter(mock=True)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "apply_patch",
                "description": "Apply a patch",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]
    assert adapter.supports_tool_calling("openai/gpt-4o") is True

    resp = adapter.complete(
        messages=[{"role": "user", "content": "Implement the logging helper."}],
        mode=LLMMode.CODING,
        tools=tools,
    )
    assert resp.mode == LLMMode.CODING
    assert "[MOCK CODING]" in resp.content
    assert len(resp.tool_calls) >= 1
    assert resp.tool_calls[0]["name"] == "apply_patch"


def test_coding_mode_blocked_when_no_tool_support():
    adapter = LLMAdapter(mock=True)
    adapter._tool_support_cache["fake/unknown-model-xyz"] = False
    tools = [
        {
            "type": "function",
            "function": {
                "name": "apply_patch",
                "description": "Apply a patch",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    try:
        adapter.complete(
            messages=[{"role": "user", "content": "write code"}],
            mode=LLMMode.CODING,
            model="fake/unknown-model-xyz",
            tools=tools,
        )
        assert False, "Expected RuntimeError for tool-incapable model"
    except RuntimeError as e:
        assert "does not support tool calling" in str(e)


def test_create_adapter_auto_mock_without_keys():
    saved = {}
    for keys in (
        ["OPENAI_API_KEY"],
        ["ANTHROPIC_API_KEY"],
        ["GOOGLE_API_KEY", "GEMINI_API_KEY"],
        ["XAI_API_KEY"],
        ["MOONSHOT_API_KEY", "KIMI_API_KEY"],
    ):
        for k in keys:
            if k in os.environ:
                saved[k] = os.environ.pop(k)
    saved_mock = os.environ.get("LLM_MOCK")
    try:
        # Flag off so factory falls back to key presence (keys removed above)
        os.environ["LLM_MOCK"] = "0"
        adapter = create_llm_adapter()
        assert adapter.mock is True
    finally:
        for k, v in saved.items():
            os.environ[k] = v
        # Restore LLM_MOCK — do NOT pop (that made later live tests see default "1")
        if saved_mock is None:
            os.environ.pop("LLM_MOCK", None)
        else:
            os.environ["LLM_MOCK"] = saved_mock


def test_mock_pass_plan_and_coding():
    """Explicit MOCK PASS assertion for CI without secrets."""
    adapter = LLMAdapter(mock=True)
    plan = adapter.complete(
        [{"role": "user", "content": "Plan a health-check endpoint."}],
        mode=LLMMode.PLAN,
    )
    coding = adapter.complete(
        [{"role": "user", "content": "Implement health-check endpoint."}],
        mode=LLMMode.CODING,
        tools=[{
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }],
    )
    assert plan.mode == LLMMode.PLAN and "[MOCK PLAN]" in plan.content
    assert coding.mode == LLMMode.CODING and "[MOCK CODING]" in coding.content
    print("MOCK PASS: Plan mode + Coding mode")


# ---------------------------------------------------------------------------
# Diagnostics + live tests
# ---------------------------------------------------------------------------

_PROVIDER_KEYS = [
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
    "GEMINI_API_KEY", "XAI_API_KEY", "MOONSHOT_API_KEY", "KIMI_API_KEY",
]


def _provider_key_names() -> list[str]:
    return [k for k in _PROVIDER_KEYS if os.getenv(k)]


def _any_provider_key() -> bool:
    return bool(_provider_key_names())


def _llm_mock_enabled() -> bool:
    return os.getenv("LLM_MOCK", "1").lower() in ("1", "true", "yes")


def print_env_diagnostics() -> None:
    """Print safe diagnostics (never prints secret values)."""
    print("--- ENV DIAGNOSTICS ---")
    print(f"cwd           : {Path.cwd()}")
    print(f"test file     : {Path(__file__).resolve()}")
    print(f"ROOT resolved : {ROOT}")
    print(f".env files loaded: {_LOADED_ENV_FILES or '(none found)'}")
    print(f"LLM_MOCK      : {os.getenv('LLM_MOCK', '<unset, default 1>')!r}")
    present = _provider_key_names()
    print(f"provider keys : {present or '(none)'}")
    for k in present:
        val = os.getenv(k) or ""
        hint = f"len={len(val)} prefix={val[:2]}…suffix={val[-2:]}" if len(val) >= 4 else f"len={len(val)}"
        print(f"  {k}: present ({hint})")
    if dotenv_values is not None:
        for path in _LOADED_ENV_FILES:
            try:
                data = dotenv_values(path)
                names = [k for k in _PROVIDER_KEYS if data.get(k)]
                extra = [k for k in (data or {}) if "KEY" in k.upper()]
                print(f"  keys inside {path}: provider={names or '(none)'} KEY-like={extra or '(none)'}")
            except Exception as exc:
                print(f"  could not parse {path}: {type(exc).__name__}")
    print("-----------------------")


def test_live_plan_mode():
    if _llm_mock_enabled():
        print("SKIP live plan: LLM_MOCK is enabled (set LLM_MOCK=0)")
        return
    if not _any_provider_key():
        print("SKIP live plan: no provider API key visible to os.getenv")
        print_env_diagnostics()
        return
    print(f"Live plan: using keys for {_provider_key_names()}")
    adapter = LLMAdapter(mock=False)
    resp = adapter.complete(
        messages=[
            {"role": "system", "content": "You are a planning assistant. Reply briefly."},
            {"role": "user", "content": "In one sentence, what is an acceptance criterion?"},
        ],
        mode=LLMMode.PLAN,
        max_tokens=64,
    )
    assert resp.content and len(resp.content) > 5
    assert resp.mode == LLMMode.PLAN
    print(f"LIVE API PASS: Plan mode via {resp.model}")


def test_live_tool_probe():
    if _llm_mock_enabled():
        print("SKIP live tool probe: LLM_MOCK is enabled")
        return
    if not _any_provider_key():
        print("SKIP live tool probe: no provider API key loaded")
        return
    adapter = LLMAdapter(mock=False)
    model = adapter.resolve_model(task="coding")
    supported = adapter.supports_tool_calling(model, force_probe=True)
    print(f"LIVE tool-calling probe for {model}: {supported}")


if __name__ == "__main__":
    print_env_diagnostics()
    test_resolve_model_defaults()
    test_plan_mode_strips_write_tools()
    test_coding_mode_mock_emits_tool_call_when_tools_provided()
    test_coding_mode_blocked_when_no_tool_support()
    test_create_adapter_auto_mock_without_keys()
    test_mock_pass_plan_and_coding()
    test_live_plan_mode()
    test_live_tool_probe()
    print("All runnable tests finished.")