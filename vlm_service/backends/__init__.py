from __future__ import annotations

import os


def make_backend(provider: str | None = None):
    """VLM 백엔드를 고른다. 기본은 OpenAI.

    provider 를 바꾸면 **판정 정확도를 다시 재야 한다** — 프롬프트는 특정 모델의
    응답을 보며 다듬은 것이고, 기록된 수치도 그 모델에서 잰 값이다.
    """
    provider = (provider or os.environ.get("VLM_PROVIDER", "openai")).lower()
    if provider == "openai":
        from vlm_service.backends.openai import OpenAIBackend

        return OpenAIBackend()
    if provider == "openrouter":
        from vlm_service.backends.openrouter import OpenRouterBackend

        return OpenRouterBackend()
    if provider == "anthropic":
        from vlm_service.backends.anthropic import AnthropicBackend

        return AnthropicBackend()
    raise ValueError(f"알 수 없는 VLM_PROVIDER: {provider}")
