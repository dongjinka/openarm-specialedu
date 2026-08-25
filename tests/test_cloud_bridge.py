"""cloud_bridge — Cloudflare DO 와 허브 사이 트랜스포트 어댑터.

순수 함수(거절 분류·프레임 라우팅)는 서버 없이, 왕복은 **진짜 오케스트레이터 + 가짜 DO** 로
검증한다. 가짜 DO 는 `pairing-session.ts` 의 중계 규칙과 거절 코드만 흉내 낸다 —
Cloudflare 없이 P0 를 돌리기 위한 것이고, 실제 배포 검증을 대신하지 않는다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket

import pytest
import uvicorn
import websockets
from websockets.http11 import Response
from websockets.datastructures import Headers

from cloud_bridge.main import (
    CloudBridge,
    classify_status,
    route_from_cloud,
)
from orchestrator.main import create_app
from orchestrator.scenario import find_scenario

SC = find_scenario("minsu_playdate_v1")
TIMEOUT = 15


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ──────────────────────────────────────────────── 거절 코드 (순수)
@pytest.mark.parametrize(
    "code,retry,counted",
    [
        (403, True, False),    # 태블릿 대기 — 설계된 순서라 계속 기다린다
        (409, True, True),     # 중복 role — 재시도하되 횟수를 센다
        (410, False, False),   # 코드 만료 — 사람이 새 코드를 줘야 한다
        (400, False, False),   # 잘못된 role
        (426, False, False),   # 업그레이드 아님
        (500, True, True),     # 예상 밖 — 세면서 재시도
    ],
)
def test_거절코드_분류(code, retry, counted):
    d = classify_status(code)
    assert d.retry is retry
    assert d.counted is counted
    assert d.reason


def test_치명적_거절은_재시도하지_않는다():
    """410/400/426 을 재시도하면 같은 답만 돌아온다 — 무한 루프의 정의다."""
    assert not any(classify_status(c).retry for c in (410, 400, 426))


# ──────────────────────────────────────────────── 프레임 라우팅 (순수)
def test_pair_이벤트는_허브로_가지_않는다():
    raw = json.dumps({"type": "pair:established", "from": "server",
                      "payload": {"connectedRoles": ["tablet", "robot"]}, "ts": 0})
    d = route_from_cloud(raw)
    assert d.forward is None                      # 도메인 이벤트가 아니다
    assert "pair:established" in d.diagnostic     # 그래도 조용히 버리지 않는다


def test_도메인_프레임은_원문_그대로_통과한다():
    raw = json.dumps({"type": "advance", "from_phase": "INTRO"})
    d = route_from_cloud(raw)
    assert d.forward == raw                       # 재직렬화하지 않는다 — 순수 전달
    assert d.diagnostic is None


def test_바이너리와_비JSON은_버리고_기록한다():
    assert route_from_cloud(b"\x00\x01\x02").forward is None
    assert route_from_cloud("not json").forward is None
    assert route_from_cloud(b"\x00").diagnostic
    assert route_from_cloud("not json").diagnostic


# ──────────────────────────────────────────────── 왕복 (진짜 허브 + 가짜 DO)
class FakeDO:
    """`pairing-session.ts` 의 계약을 흉내 낸다.

    충실히 지키는 것 세 가지 — 이것들이 브리지 동작을 좌우한다:
      1. 원문 그대로 중계 (파싱·검증 없음)
      2. 페어링 전에는 operator 측을 403 으로 막는다 (태블릿이 먼저 붙어야 한다)
      3. 연결이 바뀔 때마다 `pair:established` / `pair:peer-left` 를 브로드캐스트한다

    Cloudflare 없이 P0 를 돌리기 위한 것이고, 실제 배포 검증을 대신하지 않는다.
    """

    def __init__(self, reject_status: int | None = None):
        self.port = free_port()
        self.reject_status = reject_status
        self.peers: dict[str, websockets.ServerConnection] = {}
        self.attempts = 0
        self.ever_paired = False

    async def _process_request(self, connection, request):
        if self.reject_status is not None:
            self.attempts += 1
            return Response(self.reject_status, "rejected", Headers({"Content-Length": "0"}))
        role = request.path.split("role=")[-1]
        if role != "tablet" and not self.ever_paired and "tablet" not in self.peers:
            self.attempts += 1
            return Response(403, "tablet not connected", Headers({"Content-Length": "0"}))
        return None

    async def _broadcast_pair(self) -> None:
        roles = sorted(self.peers)
        paired = "tablet" in self.peers and len(self.peers) > 1
        if paired:
            self.ever_paired = True
        envelope = json.dumps({
            "type": "pair:established" if paired else "pair:peer-left",
            "from": "server", "payload": {"connectedRoles": roles}, "ts": 0,
        })
        for ws in list(self.peers.values()):
            with contextlib.suppress(Exception):
                await ws.send(envelope)

    async def _handler(self, ws):
        role = ws.request.path.split("role=")[-1]
        self.peers[role] = ws
        await self._broadcast_pair()
        try:
            async for raw in ws:
                for other, peer in list(self.peers.items()):
                    if other != role:
                        await peer.send(raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            self.peers.pop(role, None)
            await self._broadcast_pair()

    async def __aenter__(self):
        self.server = await websockets.serve(
            self._handler, "127.0.0.1", self.port, process_request=self._process_request
        )
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"


class Hub:
    """진짜 오케스트레이터. 가짜로 대체하지 않는다 — 허브 계약이 검증 대상이다."""

    def __init__(self, tmp_path):
        self.port = free_port()
        self.app = create_app(SC, log_dir=str(tmp_path), auto_advance=False)

    async def __aenter__(self):
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(cfg)
        self._task = asyncio.create_task(self.server.serve())
        for _ in range(300):
            if getattr(self.server, "started", False):
                break
            await asyncio.sleep(0.02)
        assert self.server.started, "오케스트레이터가 뜨지 않았다"
        return self

    async def __aexit__(self, *exc):
        self.server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(self._task, timeout=5)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"


async def _recv_until(ws, kind: str, timeout: float = TIMEOUT) -> dict:
    async def loop():
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == kind:
                return msg
        raise AssertionError(f"{kind} 를 못 받고 소켓이 닫혔다")

    return await asyncio.wait_for(loop(), timeout)


@pytest.mark.asyncio
async def test_왕복_태블릿에서_파이썬_그리고_되돌아오기(tmp_path):
    """P0 성공조건 3·4·5 — 양방향 전달이 되고, 기존 프로토콜이 그대로 통한다."""
    async with FakeDO() as do, Hub(tmp_path) as hub:
        bridge = CloudBridge(do.url, "1234", hub.url)
        task = asyncio.create_task(bridge.run())
        try:
            async with websockets.connect(f"{do.url}/api/ws/1234?role=tablet") as tablet:
                # Python → Cloudflare → 태블릿: 접속 즉시 resync(TABLET) 이 state 를 보낸다
                state = await _recv_until(tablet, "state")
                assert state["phase"] == "IDLE"
                assert "progress" in state          # 오케스트레이터 프로토콜 원형 그대로

                # 운영자가 세션을 연다 (허브에 직접 — 태블릿은 session_start 를 못 보낸다)
                async with websockets.connect(f"{hub.url}/ws/operator") as op:
                    await op.send(json.dumps({"type": "session_start",
                                              "scenario_id": SC.scenario_id}))
                    # Python → Cloudflare → 태블릿
                    intro = await _recv_until(tablet, "state")
                    assert intro["phase"] == "INTRO", intro

                    # 태블릿 → Cloudflare → Python: 유일하게 허용된 이벤트
                    await tablet.send(json.dumps({"type": "advance", "from_phase": "INTRO"}))
                    after = await _recv_until(tablet, "state")
                    assert after["phase"] == "SHOW_LIST", after
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_pair_이벤트는_허브를_오염시키지_않는다(tmp_path):
    """DO 가 만든 메시지가 허브로 올라가면 `error` 가 되돌아온다 — 그 일이 없어야 한다."""
    async with FakeDO() as do, Hub(tmp_path) as hub:
        bridge = CloudBridge(do.url, "1234", hub.url)
        task = asyncio.create_task(bridge.run())
        try:
            async with websockets.connect(f"{do.url}/api/ws/1234?role=tablet") as tablet:
                await _recv_until(tablet, "state")
                await tablet.send(json.dumps({
                    "type": "pair:established", "from": "server",
                    "payload": {"connectedRoles": ["tablet", "robot"]}, "ts": 0,
                }))
                with pytest.raises(asyncio.TimeoutError):
                    await _recv_until(tablet, "error", timeout=2.0)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_만료된_코드는_무한재시도하지_않는다(tmp_path):
    """P0 성공조건 7 — 410 을 받으면 즉시 멈춘다."""
    async with FakeDO(reject_status=410) as do, Hub(tmp_path) as hub:
        bridge = CloudBridge(do.url, "1234", hub.url)
        await asyncio.wait_for(bridge.run(), timeout=TIMEOUT)
        assert do.attempts == 1, f"410 인데 {do.attempts}회 시도했다"


@pytest.mark.asyncio
async def test_중복role은_횟수제한이_있다(tmp_path):
    """409 는 곧 풀릴 수 있으니 재시도하되, 영원히 하지는 않는다."""
    from cloud_bridge import main as m

    original = m.MAX_RECONNECT_DELAY_S, m.RECONNECT_DELAY_S, m.MAX_DUPLICATE_RETRIES
    m.MAX_RECONNECT_DELAY_S = m.RECONNECT_DELAY_S = 0.01
    m.MAX_DUPLICATE_RETRIES = 3
    try:
        async with FakeDO(reject_status=409) as do, Hub(tmp_path) as hub:
            bridge = CloudBridge(do.url, "1234", hub.url)
            await asyncio.wait_for(bridge.run(), timeout=TIMEOUT)
            assert do.attempts == 4, f"3회 재시도 후 멈춰야 하는데 {do.attempts}회"
    finally:
        m.MAX_RECONNECT_DELAY_S, m.RECONNECT_DELAY_S, m.MAX_DUPLICATE_RETRIES = original


@pytest.mark.asyncio
async def test_태블릿이_다시_붙으면_현재_state_를_받는다(tmp_path):
    """새로고침·화면잠금 회귀.

    브리지는 그 사이 허브에 계속 붙어 있으므로 `resync(TABLET)` 이 다시 나가지 않는다.
    그러면 재접속한 태블릿은 다음 전이까지 빈 화면에 남는다 — 시연에서 바로 드러난다.
    """
    async with FakeDO() as do, Hub(tmp_path) as hub:
        bridge = CloudBridge(do.url, "1234", hub.url)
        task = asyncio.create_task(bridge.run())
        try:
            async with websockets.connect(f"{do.url}/api/ws/1234?role=tablet") as tablet:
                await _recv_until(tablet, "state")
                async with websockets.connect(f"{hub.url}/ws/operator") as op:
                    await op.send(json.dumps({"type": "session_start",
                                              "scenario_id": SC.scenario_id}))
                    assert (await _recv_until(tablet, "state"))["phase"] == "INTRO"

            # 태블릿이 떠났다가 되돌아온다. 세션은 INTRO 인 채다.
            await asyncio.sleep(0.4)
            async with websockets.connect(f"{do.url}/api/ws/1234?role=tablet") as again:
                state = await _recv_until(again, "state", timeout=8)
                assert state["phase"] == "INTRO", f"재접속 태블릿이 현재 상태를 못 받았다: {state}"
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_태블릿보다_먼저_떠도_403_을_기다린다(tmp_path):
    """운용 순서: 브리지를 먼저 띄우고 태블릿이 나중에 붙는다."""
    async with FakeDO() as do, Hub(tmp_path) as hub:
        bridge = CloudBridge(do.url, "1234", hub.url)
        task = asyncio.create_task(bridge.run())
        try:
            await asyncio.sleep(0.5)
            assert do.attempts >= 1, "403 거절이 일어나야 한다"
            assert not task.done(), "403 에서 중단하면 안 된다 — 설계된 대기다"
            async with websockets.connect(f"{do.url}/api/ws/1234?role=tablet") as tablet:
                state = await _recv_until(tablet, "state", timeout=10)
                assert state["phase"] == "IDLE"
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
