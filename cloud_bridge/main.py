"""Cloudflare 트랜스포트 어댑터 — 도메인 로직은 여기 없다.

    태블릿 ──wss──► Worker ──► PairingSession(DO) ◄──wss── cloud_bridge ──ws──► 허브 /ws/tablet

DO 의 `webSocketMessage` 는 메시지를 파싱하지 않고 `peer.send(message)` 로 원문을 넘긴다.
그래서 이 프로세스도 파싱하지 않는다 — 오케스트레이터 프로토콜이 양 끝에서 그대로 통하고,
새 프로토콜도 번역 계층도 필요 없다.

여기서 하는 일은 셋뿐이다:

    1. 연결 수명 관리 (재연결·거절 코드 분류)
    2. `pair:*` 를 진단 로그로 남기고 **허브로는 올리지 않는다**
    3. 양방향 원문 전달

상태 머신·판정·로봇 결정·과제 진행은 전부 오케스트레이터에 있다.
**이 파일에 도메인 규칙을 넣지 말 것.** 넣는 순간 상태가 두 곳에 살게 된다.

허브에는 `Role.TABLET` 으로 붙는다. 실제 태블릿을 대리하기 때문이다 — 그래야
`ALLOWED_SENDERS["advance"]` 가 통과하고, 접속 즉시 `resync(TABLET)` 이 현재 `state` 를
한 번 보내 준다.

    python -m cloud_bridge.main --cloud-url ws://localhost:4173 --code 1234
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
from dataclasses import dataclass

import websockets
from websockets.exceptions import InvalidStatus

import openarm_env

logger = logging.getLogger(__name__)

#: DO 가 스스로 만들어 내는 메시지. 도메인 이벤트가 아니므로 허브로 올리지 않는다.
PAIR_PREFIX = "pair:"

#: 세션이 페어링되지 못한 채 만료됐을 때 DO 가 쓰는 close 코드.
#: (`pairing-session.ts:EXPIRED_CLOSE_CODE`) 새 코드를 받아야 한다는 뜻이다.
EXPIRED_CLOSE_CODE = 4001

RECONNECT_DELAY_S = 1.0
MAX_RECONNECT_DELAY_S = 15.0

#: 409 는 "직전 소켓이 아직 정리되지 않았다" 는 뜻이라 곧 풀린다. 그래도 무한히 두지
#: 않는다 — 정말로 다른 브리지가 붙어 있는 경우 조용히 영원히 재시도하면 안 된다.
MAX_DUPLICATE_RETRIES = 10


# ────────────────────────────────────────────────────────── 거절 코드 분류
@dataclass(frozen=True)
class Disposition:
    """DO 가 업그레이드를 거절했을 때 무엇을 할지."""

    retry: bool
    reason: str
    #: True 면 재시도 횟수를 센다 (무한 루프 방지 대상).
    counted: bool = False


def classify_status(code: int) -> Disposition:
    """`PairingSession.fetch()` 의 거절 경로를 그대로 반영한다.

    치명(retry=False)은 사람이 개입해야 풀리는 것들이다. 재시도해 봐야 같은 답이 온다.
    """
    if code == 403:
        # "tablet not connected" — 태블릿이 먼저 붙어야 한다. 설계된 순서이고,
        # 브리지를 먼저 띄우는 것이 정상 운용이므로 계속 기다린다.
        return Disposition(retry=True, reason="태블릿 대기 중 (403)")
    if code == 409:
        return Disposition(retry=True, reason="같은 role 이 이미 연결됨 (409)", counted=True)
    if code == 410:
        return Disposition(retry=False, reason="페어링 코드 만료 (410) — 태블릿에서 새 코드를 받아야 한다")
    if code == 400:
        return Disposition(retry=False, reason="잘못된 role (400)")
    if code == 426:
        return Disposition(retry=False, reason="WebSocket 업그레이드가 아니다 (426) — URL 확인")
    return Disposition(retry=True, reason=f"예상 못 한 상태 {code}", counted=True)


# ────────────────────────────────────────────────────────── 메시지 라우팅
@dataclass(frozen=True)
class RouteDecision:
    """프레임 하나를 어떻게 할지. 순수 값이라 서버 없이 테스트된다."""

    #: 그대로 넘길 원문. None 이면 넘기지 않는다.
    forward: str | None
    #: 진단 로그로 남길 내용. None 이면 남길 것 없음.
    diagnostic: str | None = None
    #: `pair:*` 가 알려 준 태블릿 존재 여부. 세션 메타데이터이지 도메인 상태가 아니다.
    tablet_present: bool | None = None


def route_from_cloud(raw: str | bytes) -> RouteDecision:
    """DO → 허브 방향. `pair:*` 만 걸러내고 나머지는 원문 그대로 통과시킨다.

    파싱은 `type` 을 읽는 데까지만 한다. 페이로드 모양을 여기서 검증하면 프로토콜이
    두 곳에 정의되는 셈이고, 허브의 `parse_inbound` 가 이미 그 일을 한다.
    """
    if isinstance(raw, (bytes, bytearray)):
        # 음성용 바이너리 프레임은 이번 PoC 범위 밖이다. 조용히 버리지 않고 남긴다.
        return RouteDecision(None, f"바이너리 프레임 {len(raw)}B — 이번 범위 밖이라 버린다")

    try:
        kind = json.loads(raw).get("type")
    except (ValueError, AttributeError):
        return RouteDecision(None, "JSON 이 아닌 프레임 — 버린다")

    if isinstance(kind, str) and kind.startswith(PAIR_PREFIX):
        roles = (json.loads(raw).get("payload") or {}).get("connectedRoles") or []
        present = "tablet" in roles
        return RouteDecision(None, f"세션 진단 {kind} roles={roles}", tablet_present=present)

    return RouteDecision(raw)


# ────────────────────────────────────────────────────────── 브리지
class CloudBridge:
    def __init__(self, cloud_url: str, code: str, hub_url: str, role: str = "robot") -> None:
        self.cloud_url = cloud_url.rstrip("/")
        self.code = code
        self.hub_url = hub_url.rstrip("/")
        self.role = role
        self._cloud: websockets.ClientConnection | None = None
        self._hub: websockets.ClientConnection | None = None
        self._stop = asyncio.Event()
        self._duplicate_retries = 0
        # 허브 연결을 태블릿의 실제 존재에 맞춘다. 오케스트레이터는 스포크가 붙는 **그 순간**
        # `resync(TABLET)` 으로 현재 state 를 한 번 보내는데, 브리지가 태블릿보다 먼저 허브에
        # 붙어 있으면 그 한 번이 허공으로 나가고 나중에 붙은 태블릿은 다음 전이까지 빈 화면에
        # 남는다. DO 도 같은 모델이다 — operator 측은 태블릿이 살아 있어야 들어올 수 있다(403).
        self._cloud_ready = asyncio.Event()
        self._tablet_present: bool | None = None

    @property
    def cloud_endpoint(self) -> str:
        return f"{self.cloud_url}/api/ws/{self.code}?role={self.role}"

    @property
    def hub_endpoint(self) -> str:
        return f"{self.hub_url}/ws/tablet"

    # ── 전달 ────────────────────────────────────────────────────────────
    async def _to_hub(self, raw: str) -> None:
        if self._hub is None:
            logger.warning("허브 미연결 — 프레임을 버린다 (%.60s)", raw)
            return
        await self._hub.send(raw)

    async def _to_cloud(self, raw: str) -> None:
        if self._cloud is None:
            # 버퍼링하지 않는다. 오래된 `state` 는 없는 것만 못하고, 재연결하면
            # 허브의 resync(TABLET) 가 현재 상태를 다시 보낸다.
            logger.warning("Cloudflare 미연결 — 프레임을 버린다 (%.60s)", raw)
            return
        await self._cloud.send(raw)

    async def _on_tablet_presence(self, present: bool) -> None:
        """태블릿이 없다가 생기면 허브 연결을 다시 맺는다.

        오케스트레이터는 스포크가 **붙는 순간**에만 `resync(TABLET)` 으로 현재 state 를
        보낸다. 브리지는 그 사이 계속 붙어 있으므로, 태블릿이 새로고침·화면잠금으로
        재접속해도 resync 가 다시 나가지 않는다 — 다음 전이까지 빈 화면이 된다.
        DO 가 알려 주는 세션 메타데이터로 그 순간을 잡아 연결을 갱신한다.
        """
        if present == self._tablet_present:
            return
        self._tablet_present = present
        if present and self._hub is not None:
            logger.info("태블릿이 (다시) 붙었다 — 허브 연결을 갱신해 resync 를 받는다")
            with contextlib.suppress(Exception):
                await self._hub.close()

    # ── Cloudflare 쪽 ───────────────────────────────────────────────────
    async def _cloud_loop(self) -> None:
        delay = RECONNECT_DELAY_S
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.cloud_endpoint, ping_interval=20) as ws:
                    self._cloud = ws
                    self._duplicate_retries = 0
                    delay = RECONNECT_DELAY_S
                    self._cloud_ready.set()
                    logger.info("Cloudflare 연결됨 role=%s code=%s", self.role, self.code)
                    async for raw in ws:
                        decision = route_from_cloud(raw)
                        if decision.diagnostic:
                            logger.info("[세션] %s", decision.diagnostic)
                        if decision.tablet_present is not None:
                            await self._on_tablet_presence(decision.tablet_present)
                        if decision.forward is not None:
                            await self._to_hub(decision.forward)
            except InvalidStatus as exc:
                status = exc.response.status_code
                disp = classify_status(status)
                if not disp.retry:
                    logger.error("Cloudflare 연결 거절 — %s. 중단한다.", disp.reason)
                    self._stop.set()
                    return
                if disp.counted:
                    self._duplicate_retries += 1
                    if self._duplicate_retries > MAX_DUPLICATE_RETRIES:
                        logger.error(
                            "Cloudflare 연결 거절 %d회 (%s) — 무한 재시도를 막기 위해 중단한다.",
                            self._duplicate_retries, disp.reason,
                        )
                        self._stop.set()
                        return
                logger.warning("Cloudflare 연결 거절 — %s. %.1f초 후 재시도.", disp.reason, delay)
            except OSError as exc:
                logger.warning("Cloudflare 연결 실패 (%s). %.1f초 후 재시도.", exc, delay)
            except websockets.ConnectionClosed as exc:
                if exc.rcvd is not None and exc.rcvd.code == EXPIRED_CLOSE_CODE:
                    logger.error("세션 만료(close %d) — 태블릿에서 새 코드를 받아야 한다. 중단한다.",
                                 EXPIRED_CLOSE_CODE)
                    self._stop.set()
                    return
                logger.warning("Cloudflare 연결 끊김 — 재연결")
            finally:
                self._cloud = None
                self._cloud_ready.clear()
                # 허브 소켓을 놓아 준다. 다시 붙을 때 resync 가 새로 나가야 하기 때문이다.
                if self._hub is not None:
                    with contextlib.suppress(Exception):
                        await self._hub.close()

            if self._stop.is_set():
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY_S)

    # ── 허브 쪽 ─────────────────────────────────────────────────────────
    async def _hub_loop(self) -> None:
        delay = RECONNECT_DELAY_S
        while not self._stop.is_set():
            # 태블릿이 붙기 전에는 허브에 붙지 않는다 (위 주석 참조).
            await self._cloud_ready.wait()
            try:
                async with websockets.connect(self.hub_endpoint, ping_interval=20) as ws:
                    self._hub = ws
                    delay = RECONNECT_DELAY_S
                    logger.info("허브 연결됨 %s", self.hub_endpoint)
                    async for raw in ws:
                        await self._to_cloud(raw)
            except OSError as exc:
                logger.warning("허브 연결 실패 (%s). %.1f초 후 재시도.", exc, delay)
            except websockets.ConnectionClosed:
                logger.warning("허브 연결 끊김 — 재연결")
            finally:
                self._hub = None

            if self._stop.is_set():
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY_S)

    async def run(self) -> None:
        logger.info("기동 — cloud=%s hub=%s", self.cloud_endpoint, self.hub_endpoint)
        tasks = [asyncio.create_task(self._cloud_loop()), asyncio.create_task(self._hub_loop())]
        stopper = asyncio.create_task(self._stop.wait())
        try:
            await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._stop.set()
            for t in [*tasks, stopper]:
                t.cancel()
            for t in [*tasks, stopper]:
                with contextlib.suppress(asyncio.CancelledError):
                    await t


def main() -> None:
    p = argparse.ArgumentParser(description="Cloudflare DO ↔ 오케스트레이터 허브 트랜스포트 어댑터")
    p.add_argument("--cloud-url", default=os.environ.get("OPENARM_CLOUD_URL"),
                   help="배포 호스트. 예: wss://openarm-special-web.<계정>.workers.dev "
                        "· 로컬은 ws://localhost:4173")
    p.add_argument("--code", default=os.environ.get("OPENARM_PAIRING_CODE"),
                   help="태블릿 화면에 뜬 4자리 코드")
    p.add_argument("--hub", default=os.environ.get("OPENARM_HUB_URL", "ws://127.0.0.1:8000"),
                   help="오케스트레이터 허브")
    p.add_argument("--role", default=os.environ.get("OPENARM_CLOUD_ROLE", "robot"),
                   help="DO 의 operator 측 슬롯 (robot | controller)")
    args = p.parse_args()

    openarm_env.load()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    missing = [n for n, v in (("--cloud-url", args.cloud_url), ("--code", args.code)) if not v]
    if missing:
        p.error(f"{' · '.join(missing)} 가 필요하다 (환경변수 OPENARM_CLOUD_URL · OPENARM_PAIRING_CODE 도 가능)")

    bridge = CloudBridge(args.cloud_url, args.code, args.hub, args.role)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(bridge.run())


if __name__ == "__main__":
    main()
