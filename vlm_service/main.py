"""VLM 서비스 — 오케스트레이터의 `vlm` · `capture` 스포크.

**소켓을 두 개 연다.** 한 프로세스지만 역할이 둘이기 때문이다.

    /ws/vlm      judge_request · verify_request  →  judge · verify_result
    /ws/capture  set_watch                       →  child_placed · zone_disturbed

역할을 합치지 않는 이유: `child_placed` 는 `capture` 만 낼 수 있다 (ALLOWED_SENDERS).
판정을 내는 쪽(`vlm`)과 판정을 **트리거하는** 쪽(`capture`)을 타입으로 갈라두면,
VLM 이 스스로 자기 판정을 부르는 경로가 생기지 않는다. 로봇에게 보낼 수 있는 것은
어느 쪽에도 하나도 없다 (§12).

    python -m vlm_service.main --frames-url http://127.0.0.1:8081/frame/latest
    python -m vlm_service.main --frames-dir /tmp/eval_set/thumbs --no-watch
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging

import websockets

from vlm_service.backends import make_backend
from vlm_service.frames import DirectoryFrameSource, HttpFrameSource
from vlm_service.service import JudgeService
from vlm_service.watcher import ZoneWatcher

logger = logging.getLogger(__name__)

#: 가방 입구가 가려졌을 때 다시 찍어보는 횟수. 팔이 물러나는 중이라 대개 1회면 걷힌다.
#: 실측에서 종료 시점 5장 중 1장이 `hidden` 이었다 — 재시도가 없으면 그 20%가 운영자에게 간다.
VERIFY_RETRIES = 2
VERIFY_RETRY_DELAY_S = 1.2


class _Socket:
    """재연결을 견디는 전송 래퍼. 콜백이 항상 '지금 살아 있는' 소켓을 쓰게 한다."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._ws = None

    def bind(self, ws) -> None:
        self._ws = ws

    async def send(self, payload: dict) -> None:
        if self._ws is None:
            logger.warning("%s 소켓이 아직 없다 — 버림: %s", self.name, payload.get("type"))
            return
        await self._ws.send(json.dumps(payload, ensure_ascii=False))


async def _serve(url: str, sock: _Socket, on_message) -> None:
    async for ws in websockets.connect(url, ping_interval=20):
        sock.bind(ws)
        try:
            logger.info("연결됨 %s", url)
            async for raw in ws:
                await on_message(json.loads(raw))
        except websockets.ConnectionClosed:
            logger.warning("연결 끊김 (%s) — 재연결", url)
            continue
        finally:
            sock.bind(None)


class VlmService:
    def __init__(self, service: JudgeService, frames, *,
                 checklist: list[str], watch: bool = True) -> None:
        self.service = service
        self.frames = frames
        self.checklist = checklist
        self.packed: list[str] = []
        self.watch_enabled = watch
        self.vlm_sock = _Socket("vlm")
        self.capture_sock = _Socket("capture")
        self.watcher = ZoneWatcher(frames, self._perceive_for_watch, self.capture_sock.send)

    # ── capture 스포크 ───────────────────────────────────────────────────
    async def on_capture(self, msg: dict) -> None:
        if msg.get("type") == "set_watch":
            if self.watch_enabled:
                self.watcher.set_mode(msg.get("mode", "off"))

    async def _perceive_for_watch(self, image: bytes) -> dict | None:
        """안정된 프레임 1장 → 판정. 배치면이 비었으면 None (아이가 치운 것)."""
        verdict = await self.service.judge(image, self.checklist, self.packed)
        if verdict.object in ("none", ""):
            return None
        if verdict.hold_for_operator:
            # 보류도 **판정과 똑같이 취급한다.** 여기서 vlm_hold 를 직접 보내고 끝내면
            # 오케스트레이터는 WAIT_CHILD 에 머무는데, vlm_hold 는 JUDGE 에서만
            # 처리되므로 이벤트가 조용히 버려지고 **운영자가 호출되지 않는다.**
            # 실측에서 보류는 45장 중 15건이었다 — 드문 경로가 아니다.
            return {"type": "vlm_hold", "object": verdict.object,
                    "confidence": verdict.confidence, "reason": verdict.reason}
        return {"type": "judge", "object": verdict.object,
                "should_pack": verdict.should_pack, "confidence": verdict.confidence}

    # ── vlm 스포크 ───────────────────────────────────────────────────────
    async def on_vlm(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "judge_request":
            await self._judge(msg)
        elif kind == "verify_request":
            await self._verify(msg)

    async def _judge(self, msg: dict) -> None:
        self.checklist = msg.get("checklist") or self.checklist
        self.packed = list(msg.get("packed") or [])

        cached = self.watcher.cached()
        if cached is not None:
            # watcher 가 방금 안정 프레임으로 판정했고 그 뒤 장면이 그대로다.
            # 다시 찍으면 호출이 2회가 되고 그 사이 장면이 바뀌는 레이스가 생긴다.
            # 보류(vlm_hold)도 그대로 흘린다 — 그래야 JUDGE 의 보류 경로가 돈다.
            logger.info("캐시 사용 %s object=%s", cached.get("type"), cached.get("object"))
            await self.vlm_sock.send(cached)
            return

        image = await self.frames.latest()
        verdict = await self.service.judge(image, self.checklist, self.packed)
        logger.info("판정 object=%s should_pack=%s conf=%.2f hold=%s (%.2fs) — %s",
                    verdict.object, verdict.should_pack, verdict.confidence,
                    verdict.hold_for_operator, self.service.last_latency_s or 0.0,
                    verdict.reason)
        if verdict.hold_for_operator:
            await self.vlm_sock.send({"type": "vlm_hold", "object": verdict.object,
                                      "confidence": verdict.confidence,
                                      "reason": verdict.reason})
            return
        await self.vlm_sock.send({"type": "judge", "object": verdict.object,
                                  "should_pack": verdict.should_pack,
                                  "confidence": verdict.confidence})

    async def _verify(self, msg: dict) -> None:
        """사후 확인 — 배치면이 비었는지와 **가방이 닫혔는지**를 따로 본다.

        예전 구현은 "배치면이 비었다"는 신호 하나를 `object_in_bag` 과 `bag_closed`
        양쪽에 넣었다. 가방이 열린 채 끝난 턴이 성공으로 기록됐고, 그 상태가 다음 턴의
        시작 관측을 분포 밖으로 만든다 (에피소드는 가방이 닫힌 채 시작한다).
        """
        cmd_id = msg.get("cmd_id", "")
        verdict = None
        for attempt in range(VERIFY_RETRIES + 1):
            image = await self.frames.latest()
            verdict = await self.service.verify(image)
            if not verdict.hold_for_operator:
                break
            if attempt < VERIFY_RETRIES:
                logger.info("가방 상태를 못 봤다 — 재시도 %d/%d", attempt + 1, VERIFY_RETRIES)
                await asyncio.sleep(VERIFY_RETRY_DELAY_S)

        logger.info("사후확인 in_bag=%s closed=%s conf=%.2f hold=%s — %s",
                    verdict.object_in_bag, verdict.bag_closed, verdict.confidence,
                    verdict.hold_for_operator, verdict.reason)

        if verdict.hold_for_operator:
            # 가방 상태를 못 봤다. 닫혔다고 추측하면 열린 채 다음 턴이 시작되고,
            # 열렸다고 하면 성공한 턴이 아동에게 '로봇의 실수'로 안내된다. 사람이 정한다.
            await self.vlm_sock.send({"type": "operator_attention", "reason": "verify_hold",
                                      "cmd_id": cmd_id,
                                      "object_in_bag": verdict.object_in_bag,
                                      "detail": verdict.reason})
            return
        await self.vlm_sock.send({"type": "verify_result", "cmd_id": cmd_id,
                                  "object_in_bag": verdict.object_in_bag,
                                  "bag_closed": verdict.bag_closed})

    async def run(self, base: str) -> None:
        tasks = [asyncio.create_task(_serve(f"{base}/ws/vlm", self.vlm_sock, self.on_vlm))]
        if self.watch_enabled:
            tasks.append(asyncio.create_task(
                _serve(f"{base}/ws/capture", self.capture_sock, self.on_capture)))
        try:
            await asyncio.gather(*tasks)
        finally:
            await self.watcher.aclose()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://127.0.0.1:8000")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--frames-dir", default=None)
    src.add_argument("--frames-url", default=None)
    p.add_argument("--provider", default=None)
    p.add_argument("--min-confidence", type=float, default=0.70)
    p.add_argument("--checklist", default="flower,whale,tree")
    p.add_argument("--no-watch", action="store_true",
                   help="상시 감지를 끄고 운영자 버튼 트리거만 쓴다 (폴백)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    frames = (HttpFrameSource(args.frames_url) if args.frames_url
              else DirectoryFrameSource(args.frames_dir or "/tmp/eval_set/thumbs"))
    service = JudgeService(make_backend(args.provider), min_confidence=args.min_confidence)
    svc = VlmService(service, frames,
                     checklist=[x.strip() for x in args.checklist.split(",") if x.strip()],
                     watch=not args.no_watch)
    logger.info("기동 (backend=%s, frames=%s, watch=%s)",
                service.backend.name, frames.name, not args.no_watch)
    # 예전 사용법은 --url 에 전체 경로(.../ws/vlm)를 줬다. 이제는 두 소켓을 열므로
    # 베이스만 필요하다. 옛 형태가 들어와도 조용히 받아준다.
    base = args.url.rstrip("/")
    for suffix in ("/ws/vlm", "/ws/capture"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(svc.run(base))


if __name__ == "__main__":
    main()
