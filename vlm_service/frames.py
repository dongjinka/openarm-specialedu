"""프레임 공급자.

§4.3 — `capture_service` 가 카메라를 독점하고 배포한다. VLM 은 판정 시점에
HTTP 로 1장만 받아간다. 카메라를 직접 열지 않는다 (§12 안티패턴).

`capture_service` 가 아직 없는 동안에는 디렉터리 공급자로 대체한다.
"""

from __future__ import annotations

import io
import itertools
from pathlib import Path
from typing import Protocol

import httpx

#: 실측 (gemini-3.7-flash, 12장):
#:   512px 중앙 4.04s / 320px 중앙 3.55s (예측 12/12 동일) / 224px 중앙 4.06s (11/12, tree→other)
#: 업로드 크기는 병목이 아니다. 320 은 공짜로 조금 빠르고 정확도 손실이 없다.
#: 224 밑으로는 내리지 말 것 — 물체가 작아 형태 단서가 무너진다.
MAX_WIDTH = 320


def downscale(data: bytes, max_width: int = MAX_WIDTH) -> bytes:
    """가로가 max_width 를 넘으면 줄인다. 넘지 않으면 원본 그대로."""
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    if img.width <= max_width:
        return data
    img = img.convert("RGB")
    img = img.resize((max_width, int(img.height * max_width / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


class FrameSource(Protocol):
    name: str

    async def latest(self) -> bytes: ...


class HttpFrameSource:
    """capture_service 의 GET /frame/latest."""

    def __init__(self, url: str, timeout: float = 5.0) -> None:
        self.url = url
        self.timeout = timeout
        self.name = f"http:{url}"

    async def latest(self) -> bytes:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(self.url)
            resp.raise_for_status()
            return downscale(resp.content)


class DirectoryFrameSource:
    """디렉터리의 이미지를 순서대로 돌려준다. capture_service 없이 배선을 검증한다."""

    def __init__(self, directory: str | Path, pattern: str = "*.jpg") -> None:
        self.paths = sorted(Path(directory).glob(pattern))
        if not self.paths:
            raise FileNotFoundError(f"이미지 없음: {directory}/{pattern}")
        self._cycle = itertools.cycle(self.paths)
        self.name = f"dir:{directory}"

    async def latest(self) -> bytes:
        return downscale(next(self._cycle).read_bytes())
