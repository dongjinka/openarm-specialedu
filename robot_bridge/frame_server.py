"""씬 카메라 프레임 배포 — `GET /frame/latest`.

**`capture_service` 를 별도 프로세스로 만들지 않는다.** 카메라를 여는 프로세스는
`robot_bridge` 하나여야 한다 (§12 "카메라를 두 프로세스가 직접 열기" 금지). GR00T 는
카메라 3대를 관측으로 쓰고, VLM 은 그중 씬 카메라 1장만 판정 시점에 받아 가면 된다.
그래서 로봇을 소유한 쪽이 HTTP 로 나눠 준다.

`vlm_service/frames.py:HttpFrameSource` 가 처음부터 이 엔드포인트를 기다리고 있었다.
"""

from __future__ import annotations

import asyncio
import contextlib
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
    """취소되면 uvicorn 을 **스스로 접게** 한다.

    태스크를 그냥 취소하면 uvicorn 의 lifespan 태스크가 `CancelledError` 트레이스백을
    ERROR 로 찍는다. 종료 시점에 정작 봐야 할 것은 그 밑에 오는 teardown 로그
    (시작 자세 복귀 · `arms lowered and released` · `torque released`)인데,
    그것을 묻어 버린다. `should_exit` 로 접으면 조용히 끝난다.
    """
    import uvicorn

    config = uvicorn.Config(build_app(backend), host=host, port=port,
                            log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    # 우리가 신호를 소유한다 — uvicorn 이 자기 핸들러를 걸면 그쪽이 이긴다.
    server.install_signal_handlers = lambda: None
    task = asyncio.create_task(server.serve())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=5)
        raise
