"""Shared integration factories; adapters remain the single implementation."""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Tuple


@lru_cache(maxsize=1)
def main_llm_runtime() -> Tuple[object, str]:
    from adapters.llm_adapter import ModelProvider, create_llm_adapter

    provider_name = os.getenv("LLM_PROVIDER", "gemini").lower()
    try:
        provider = ModelProvider(provider_name)
    except ValueError:
        provider = ModelProvider.GEMINI
    adapter = create_llm_adapter()
    # Explicitly pin the exact same model for Plan, Coding and Reviewer modes.
    model = os.getenv("LLM_MODEL") or adapter.resolve_model(provider=provider)
    return adapter, model


@lru_cache(maxsize=4)
def memorylake_adapter(project_id: str | None, mock_flag: str):
    from adapters.memorylake_adapter import create_memorylake_adapter
    mock = mock_flag.lower() in ("1", "true", "yes") if mock_flag else None
    return create_memorylake_adapter(mock=mock, project_id=project_id)


def get_memorylake_adapter():
    project_id = os.getenv("MEMORYLAKE_PROJECT_ID")
    mock_flag = os.getenv("MEMORYLAKE_MOCK", "")
    return memorylake_adapter(project_id, mock_flag)


def clear_runtime_caches() -> None:
    main_llm_runtime.cache_clear()
    memorylake_adapter.cache_clear()
