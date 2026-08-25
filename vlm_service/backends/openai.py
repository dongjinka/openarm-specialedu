"""OpenAI 백엔드.

OpenRouter 와 같은 `/chat/completions` 스키마라 요청 모양은 같다. 다른 것은
주소·키·모델명뿐이다.

⚠️ **모델을 바꾸면 판정 정확도를 다시 재야 한다.** 기록된 20/20 · 10/10 은
gemini-3.7-flash 로 잰 값이고, 프롬프트도 그 모델의 응답을 보며 다듬은 것이다.
`tools/eval_vlm.py` 로 재측정한 뒤에 그 숫자를 쓴다.
"""

from __future__ import annotations

import os

import httpx

from vlm_service.backends.base import data_url

#: 비전을 받는 저지연 모델. 실측 후 필요하면 OPENAI_MODEL 로 바꾼다.
DEFAULT_MODEL = "gpt-4o-mini"


class OpenAIBackend:
    def __init__(self, api_key: str | None = None, model: str | None = None,
                 base_url: str | None = None, timeout: float = 60.0) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model or os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL",
                                                    "https://api.openai.com/v1")).rstrip("/")
        self.timeout = timeout
        self.name = f"openai:{self.model}"

    async def infer(self, image_bytes: bytes, system: str, user: str) -> str:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY 가 없다 (.env 를 확인한다)")
        payload = {
            "model": self.model,
            # 같은 장면에 같은 답이 나와야 한다. 판정이 흔들리면 재현이 안 된다.
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
