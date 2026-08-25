"""Anthropic 백엔드. OpenRouter 를 거치지 않고 직접 부를 때."""

from __future__ import annotations

import base64
import os

import httpx

from vlm_service.backends.base import encode

DEFAULT_MODEL = "claude-sonnet-5"


class AnthropicBackend:
    def __init__(self, api_key: str | None = None, model: str | None = None,
                 base_url: str | None = None, timeout: float = 60.0) -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or os.environ.get("VLM_MODEL", DEFAULT_MODEL)
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL",
                                                    "https://api.anthropic.com")).rstrip("/")
        self.timeout = timeout
        self.name = f"anthropic:{self.model}"

    async def infer(self, image_bytes: bytes, system: str, user: str) -> str:
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY 가 없다")
        data, mime = encode(image_bytes)
        payload = {
            "model": self.model,
            "max_tokens": 256,
            "temperature": 0,
            "system": system,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime,
                                             "data": base64.b64encode(data).decode()}},
                {"type": "text", "text": user},
            ]}],
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/v1/messages",
                headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                json=payload,
            )
            resp.raise_for_status()
            blocks = resp.json()["content"]
            return "".join(b.get("text", "") for b in blocks)
