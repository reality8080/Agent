"""
Unified LLM Adapter using LiteLLM.

Supports: OpenAI, Anthropic (Claude), Google (Gemini), xAI (Grok), Moonshot (Kimi).
Enforces Plan mode (read-only) vs Coding mode (write tools only after Gate 1).
Does NOT embed API keys. Keys must come from environment variables.
"""

from __future__ import annotations
from dotenv import load_dotenv
load_dotenv()
import json
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Enums & Config
# ---------------------------------------------------------------------------


class LLMMode(str, Enum):
    """Permission modes for the Main LLM."""

    PLAN = "plan"       # read-only: research, modeling, planning
    CODING = "coding"   # write-scoped: only after Gate 1 approval


class ModelProvider(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    GROK = "grok"
    MOONSHOT = "moonshot"  # Kimi


# Default model identifiers understood by LiteLLM
DEFAULT_MODELS: Dict[ModelProvider, str] = {
    ModelProvider.OPENAI: "openai/gpt-4o",
    ModelProvider.ANTHROPIC: "anthropic/claude-3-5-sonnet-20241022",
    ModelProvider.GEMINI: "gemini/gemini-3.5-flash-lite",
    ModelProvider.GROK: "xai/grok-2-latest",
    ModelProvider.MOONSHOT: "moonshot/moonshot-v1-32k",
}

# Env var names that LiteLLM / providers expect
PROVIDER_ENV_KEYS: Dict[ModelProvider, List[str]] = {
    ModelProvider.OPENAI: ["OPENAI_API_KEY"],
    ModelProvider.ANTHROPIC: ["ANTHROPIC_API_KEY"],
    ModelProvider.GEMINI: ["GOOGLE_API_KEY", "GEMINI_API_KEY"],
    ModelProvider.GROK: ["XAI_API_KEY"],
    ModelProvider.MOONSHOT: ["MOONSHOT_API_KEY", "KIMI_API_KEY"],
}


@dataclass
class ToolSpec:
    """Minimal tool description for capability probing."""

    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    content: str
    model: str
    mode: LLMMode
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    usage: Optional[Dict[str, Any]] = None
    raw: Any = None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class LLMAdapter:
    """
    Unified adapter over LiteLLM.

    - One interface for all supported providers.
    - Explicit Plan vs Coding mode.
    - Tool-calling capability probe before Coding mode is allowed.
    - Never stores or logs API keys.
    """

    def __init__(
        self,
        default_provider: ModelProvider = ModelProvider.OPENAI,
        model_overrides: Optional[Dict[str, str]] = None,
        temperature: float = 1.0,
        max_tokens: int = 4096,
        mock: bool = False,
    ):
        """
        Args:
            default_provider: Fallback provider when task does not specify one.
            model_overrides: Optional mapping provider_name -> litellm model id.
            temperature / max_tokens: Generation defaults.
            mock: When True, never call real APIs; return deterministic mock data.
        """
        self.default_provider = default_provider
        self.model_overrides = model_overrides or {}
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.mock = mock or os.getenv("LLM_MOCK", "").lower() in ("1", "true", "yes")

        # Cache of tool-calling support per model id
        self._tool_support_cache: Dict[str, bool] = {}

        if not self.mock:
            try:
                import litellm  # noqa: F401
            except ImportError as e:
                raise ImportError(
                    "litellm is required. Install with: pip install litellm"
                ) from e

    # ------------------------------------------------------------------
    # Model resolution
    # ------------------------------------------------------------------

    def resolve_model(
        self,
        provider: Optional[ModelProvider | str] = None,
        task: Optional[str] = None,
    ) -> str:
        """
        Resolve LiteLLM model identifier.

        Priority:
        1. Explicit provider argument
        2. Task-specific env (e.g. LLM_MODEL_PLAN, LLM_MODEL_CODING)
        3. Provider default
        """
        if isinstance(provider, str):
            try:
                provider = ModelProvider(provider.lower())
            except ValueError:
                # Treat as raw LiteLLM model string
                return provider

        if provider is None:
            # Task-based selection
            if task == "plan":
                env_model = os.getenv("LLM_MODEL_PLAN")
                if env_model:
                    return env_model
            elif task == "coding":
                env_model = os.getenv("LLM_MODEL_CODING")
                if env_model:
                    return env_model
            provider = self.default_provider

        assert isinstance(provider, ModelProvider)
        override = self.model_overrides.get(provider.value)
        if override:
            return override
        return DEFAULT_MODELS[provider]

    def has_api_key(self, provider: ModelProvider) -> bool:
        """Return True if at least one expected env var for the provider is set."""
        for key in PROVIDER_ENV_KEYS.get(provider, []):
            if os.getenv(key):
                return True
        return False

    # ------------------------------------------------------------------
    # Tool-calling capability probe
    # ------------------------------------------------------------------

    def supports_tool_calling(self, model: str, force_probe: bool = False) -> bool:
        """
        Check whether the model can perform tool / function calling.

        In mock mode always returns True for known coding-capable families.
        Live probe uses a minimal tools list and inspects the response shape.
        Result is cached per model id.
        """
        if model in self._tool_support_cache and not force_probe:
            return self._tool_support_cache[model]

        if self.mock:
            # Conservative heuristic for mock
            known_capable = any(
                token in model.lower()
                for token in (
                    "gpt-4",
                    "gpt-3.5",
                    "claude",
                    "gemini",
                    "grok",
                    "moonshot",
                    "qwen",
                    "deepseek",
                )
            )
            self._tool_support_cache[model] = known_capable
            return known_capable

        try:
            import litellm

            probe_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "ping",
                        "description": "Simple capability probe",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "msg": {"type": "string"},
                            },
                            "required": ["msg"],
                        },
                    },
                }
            ]
            resp = litellm.completion(
                model=model,
                messages=[{"role": "user", "content": "Call the ping tool with msg='ok'."}],
                tools=probe_tools,
                tool_choice="auto",
                max_tokens=64,
                temperature=0,
            )
            # LiteLLM normalises tool_calls onto the message
            message = resp.choices[0].message
            has_tools = bool(getattr(message, "tool_calls", None))
            self._tool_support_cache[model] = has_tools
            logger.info("Tool-calling probe for %s → %s", model, has_tools)
            return has_tools
        except Exception as exc:
            logger.warning("Tool-calling probe failed for %s: %s", model, exc)
            self._tool_support_cache[model] = False
            return False

    # ------------------------------------------------------------------
    # Core completion
    # ------------------------------------------------------------------

    def complete(
        self,
        messages: Sequence[Dict[str, str]],
        *,
        mode: LLMMode = LLMMode.PLAN,
        provider: Optional[ModelProvider | str] = None,
        model: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """
        Run a completion under the given mode.

        Hard rules enforced here:
        - PLAN mode: tools that imply write/file mutation are stripped.
        - CODING mode: only allowed after external Gate 1 (caller responsibility);
          this adapter still refuses to attach write tools if the model
          fails the tool-calling probe.
        """
        resolved_model = model or self.resolve_model(provider, task=mode.value)

        # Safety: never attach write-capable tools in PLAN mode
        safe_tools = self._filter_tools_for_mode(tools, mode)

        if mode == LLMMode.CODING and safe_tools:
            if not self.supports_tool_calling(resolved_model):
                raise RuntimeError(
                    f"Model '{resolved_model}' does not support tool calling. "
                    "Coding mode with tools is blocked until a capable model is configured."
                )

        if self.mock:
            return self._mock_complete(
                messages=messages,
                model=resolved_model,
                mode=mode,
                tools=safe_tools,
            )

        import litellm

        # Soften LiteLLM noise
        litellm.suppress_debug_info = True

        try:
            resp = litellm.completion(
                model=resolved_model,
                messages=list(messages),
                tools=safe_tools or None,
                temperature=temperature if temperature is not None else self.temperature,
                max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
                **kwargs,
            )
        except Exception as exc:
            logger.error("LiteLLM completion failed (model=%s): %s", resolved_model, type(exc).__name__)
            raise

        message = resp.choices[0].message
        content = getattr(message, "content", None) or ""
        tool_calls_raw = getattr(message, "tool_calls", None) or []

        parsed_tool_calls = []
        for tc in tool_calls_raw:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            args = getattr(fn, "arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            parsed_tool_calls.append(
                {
                    "id": getattr(tc, "id", None),
                    "name": getattr(fn, "name", None),
                    "arguments": args,
                }
            )

        usage = None
        if getattr(resp, "usage", None):
            usage = {
                "prompt_tokens": getattr(resp.usage, "prompt_tokens", None),
                "completion_tokens": getattr(resp.usage, "completion_tokens", None),
                "total_tokens": getattr(resp.usage, "total_tokens", None),
            }

        return LLMResponse(
            content=content,
            model=resolved_model,
            mode=mode,
            tool_calls=parsed_tool_calls,
            usage=usage,
            raw=resp,
        )

    def _filter_tools_for_mode(
        self,
        tools: Optional[List[Dict[str, Any]]],
        mode: LLMMode,
    ) -> Optional[List[Dict[str, Any]]]:
        if not tools:
            return None
        if mode == LLMMode.PLAN:
            # Strip any tool whose name suggests mutation
            write_indicators = (
                "write", "edit", "create_file", "delete", "remove",
                "patch", "apply", "run_terminal", "execute", "bash",
            )
            filtered = []
            for t in tools:
                name = ""
                if "function" in t:
                    name = t["function"].get("name", "")
                elif "name" in t:
                    name = t.get("name", "")
                if any(ind in name.lower() for ind in write_indicators):
                    logger.debug("Stripped write tool '%s' in PLAN mode", name)
                    continue
                filtered.append(t)
            return filtered or None
        return tools

    # ------------------------------------------------------------------
    # Mock implementation (no network, no keys)
    # ------------------------------------------------------------------

    def _mock_complete(
        self,
        messages: Sequence[Dict[str, str]],
        model: str,
        mode: LLMMode,
        tools: Optional[List[Dict[str, Any]]],
    ) -> LLMResponse:
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = m.get("content", "")
                break

        if mode == LLMMode.PLAN:
            content = (
                f"[MOCK PLAN] Model={model}\n"
                f"Understood request: {last_user[:200]}...\n\n"
                "## Problem Brief\n"
                "- Goal: (mock) implement the requested feature\n"
                "- Scope: single module change\n"
                "- Acceptance: unit tests pass\n\n"
                "## Implementation Plan\n"
                "1. Read existing code\n"
                "2. Minimal patch\n"
                "3. Run tests\n"
            )
            tool_calls = []
        else:
            content = (
                f"[MOCK CODING] Model={model}\n"
                f"Would generate patch for: {last_user[:120]}...\n"
            )
            tool_calls = []
            if tools:
                # Simulate a single tool call if tools were offered
                first = tools[0]
                fname = first.get("function", {}).get("name") or first.get("name", "unknown")
                tool_calls = [
                    {
                        "id": "mock_call_1",
                        "name": fname,
                        "arguments": {"path": "src/example.py", "content": "# mock patch"},
                    }
                ]

        return LLMResponse(
            content=content,
            model=model,
            mode=mode,
            tool_calls=tool_calls,
            usage={"prompt_tokens": 42, "completion_tokens": 80, "total_tokens": 122},
            raw={"mock": True},
        )


# Convenience factory used by higher layers
def create_llm_adapter(mock: Optional[bool] = None) -> LLMAdapter:
    """Create adapter respecting LLM_MOCK / presence of any key."""
    if mock is None:
        mock = os.getenv("LLM_MOCK", "").lower() in ("1", "true", "yes")
        # Auto-mock when no provider key is present
        if not mock:
            any_key = any(
                os.getenv(k)
                for keys in PROVIDER_ENV_KEYS.values()
                for k in keys
            )
            mock = not any_key
    return LLMAdapter(mock=mock)
