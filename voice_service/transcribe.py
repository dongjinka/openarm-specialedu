"""음성 → 텍스트. 벤더 교체가 서비스 코드를 건드리지 않게 한다.

CLOVA 를 기본으로 둔 것은 실측 때문이다 — 로컬 CPU whisper 는 어떤 모델도 예산에
못 들어왔고(`large-v3-turbo` 17.9초/클립, 예산의 6배), CLOVA 가 **861ms** 로 해결했다
(`openarm-sciedu/voice-pipeline/SUMMARY.md`).

주의: 이걸로 네트워크 의존이 STT·VLM 둘로 늘어난다. VLM 이 죽으면 운영자 수동 판정으로
강등되지만, STT 가 죽으면 그냥 조용해질 뿐이다 — 세션은 그대로 돈다.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol

logger = logging.getLogger(__name__)

#: 2026-08-11 공식 문서에서 확인한 값 (openarmsciedu-omni/stt/config/clova_speech_short.json).
CLOVA_URL = "https://clovaspeech-gw.ncloud.com/recog/v1/stt?lang=Kor"


class Transcriber(Protocol):
    name: str

    async def transcribe(self, wav: bytes) -> str: ...


class ClovaTranscriber:
    """CLOVA Speech 짧은 문장 인식. wav 를 그대로 본문에 싣는다."""

    name = "clova"

    def __init__(self, secret: str | None = None, url: str | None = None,
                 timeout: float = 20.0) -> None:
        self.secret = secret or os.environ.get("CLOVA_SPEECH_SECRET", "")
        self.url = url or os.environ.get("CLOVA_SPEECH_URL", CLOVA_URL)
        self.timeout = timeout

    async def transcribe(self, wav: bytes) -> str:
        import httpx

        if not self.secret:
            raise RuntimeError("CLOVA_SPEECH_SECRET 가 없다")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                self.url, content=wav,
                headers={"X-CLOVASPEECH-API-KEY": self.secret,
                         "Content-Type": "application/octet-stream"},
            )
            resp.raise_for_status()
            return str(resp.json().get("text", "")).strip()


class NullTranscriber:
    """항상 빈 문자열. 마이크나 키가 없을 때 배선만 확인한다."""

    name = "null"

    def __init__(self, text: str = "") -> None:
        self.text = text

    async def transcribe(self, wav: bytes) -> str:  # noqa: ARG002
        return self.text


def make_transcriber(provider: str | None = None) -> Transcriber:
    provider = (provider or os.environ.get("STT_PROVIDER", "clova")).lower()
    if provider == "clova":
        return ClovaTranscriber()
    if provider == "null":
        return NullTranscriber()
    raise ValueError(f"알 수 없는 STT_PROVIDER: {provider}")
