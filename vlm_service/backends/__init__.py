from __future__ import annotations

import os


def make_backend(provider: str | None = None):
    provider = (provider or os.environ.get("VLM_PROVIDER", "openrouter")).lower()
    if provider == "openrouter":
        from vlm_service.backends.openrouter import OpenRouterBackend

        return OpenRouterBackend()
    if provider == "anthropic":
        from vlm_service.backends.anthropic import AnthropicBackend

        return AnthropicBackend()
    raise ValueError(f"알 수 없는 VLM_PROVIDER: {provider}")
