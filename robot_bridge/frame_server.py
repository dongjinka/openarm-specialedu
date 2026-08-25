"""씬 카메라 프레임 배포 — `GET /frame/latest`.

**`capture_service` 를 별도 프로세스로 만들지 않는다.** 카메라를 여는 프로세스는
`robot_bridge` 하나여야 한다 (§12 "카메라를 두 프로세스가 직접 열기" 금지). GR00T 는
카메라 3대를 관측으로 쓰고, VLM 은 그중 씬 카메라 1장만 판정 시점에 받아 가면 된다.
그래서 로봇을 소유한 쪽이 HTTP 로 나눠 준다.

`vlm_service/frames.py:HttpFrameSource` 가 처음부터 이 엔드포인트를 기다리고 있었다.
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Response

logger = logging.getLogger(__name__)

#: 데이터셋 씬 카메라 키. 관측 dict 에서 이 이름으로 찾는다.
SCENE_KEY = "observation.images.follower_d455f"
SCENE_ALIASES = (SCENE_KEY, "follower_d455f", "scene")


def to_jpeg(frame: Any, quality: int = 85) -> bytes:
    """카메라 프레임(ndarray · 텐서 · bytes) → JPEG 바이트."""
    from PIL import Image

    if isinstance(frame, (bytes, bytearray)):
        return bytes(frame)

    array = frame
    if hasattr(array, "detach"):                 # torch 텐서
        array = array.detach().cpu().numpy()
    if hasattr(array, "ndim") and array.ndim == 4:
        array = array[0]                         # 배치 차원
    if hasattr(array, "ndim") and array.ndim == 3 and array.shape[0] in (1, 3):
        array = array.transpose(1, 2, 0)         # CHW → HWC
    if hasattr(array, "dtype") and str(array.dtype).startswith("float"):
        import numpy as np

        array = (np.clip(array, 0.0, 1.0) * 255).astype("uint8")

    img = Image.fromarray(array)
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def build_app(backend) -> FastAPI:
    app = FastAPI(title="robot_bridge frame server")

    @app.get("/frame/latest")
    async def latest() -> Response:
        getter = getattr(backend, "latest_frame", None)
        if getter is None:
            raise HTTPException(status_code=503, detail="이 백엔드는 프레임을 내지 않는다")
        try:
            data = await asyncio.to_thread(getter)
        except Exception as exc:  # noqa: BLE001
            logger.warning("프레임 획득 실패: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if data is None:
            raise HTTPException(status_code=503, detail="아직 프레임이 없다")
        return Response(content=data, media_type="image/jpeg")

    @app.get("/health")
    async def health() -> dict:
        return {"backend": getattr(backend, "name", "?"),
                "frames": hasattr(backend, "latest_frame")}

    return app


async def serve(backend, host: str, port: int) -> None:
    import uvicorn

    config = uvicorn.Config(build_app(backend), host=host, port=port,
                            log_level="warning", access_log=False)
    await uvicorn.Server(config).serve()
