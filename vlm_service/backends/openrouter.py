"""OpenRouter 백엔드. omni 프로젝트가 이미 쓰던 경로를 그대로 따른다."""

from __future__ import annotations

import os

import httpx

from vlm_service.backends.base import data_url

DEFAULT_MODEL = "google/gemini-3.7-flash"


class OpenRouterBackend:
    def __init__(self, api_key: str | None = None, model: str | None = None,
                 base_url: str | None = None, timeout: float = 60.0) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.model = model or os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)
        self.base_url = (base_url or os.environ.get("OPENROUTER_BASE_URL",
                                                    "https://openrouter.ai/api/v1")).rstrip("/")
        self.timeout = timeout
        self.name = f"openrouter:{self.model}"

    async def infer(self, image_bytes: bytes, system: str, user: str) -> str:
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY 가 없다")
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": [
                    {"type": "text", "text": user},
                    {"type": "image_url", "image_url": {"url": data_url(image_bytes)}},
                ]},
            ],
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
