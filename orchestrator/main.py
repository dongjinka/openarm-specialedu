"""오케스트레이터 — 이벤트 허브이자 유일한 제어 흐름 경유지.

상태 머신은 순수하고, 여기서 부작용을 실행한다:
Effect 디스패치 · 타이머 소유 · JSONL 기록 · WS 배선.

발화·애니메이션이 끝났다는 `advance` 는 원래 태블릿이 보낸다. 태블릿이 아직 없는
1단계에서는 `auto_advance` 로 대신해 `--sim` 로봇만으로 전 구간이 돌게 한다.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from orchestrator import events as ev
from orchestrator.effects import (
    Broadcast,
    CancelTimer,
    Log,
    RequestJudge,
    RequestVerify,
    SetListen,
    SetWatch,
    SendRobot,
    StartTimer,
)
from orchestrator.hub import Hub
from orchestrator.logger import EpisodeLogger
from orchestrator.machine import SessionState, handle
from orchestrator.scenario import Scenario, find_scenario
from orchestrator.states import Phase

logger = logging.getLogger(__name__)

#: 발화·애니메이션이 끝나면 스스로 넘어가는 상태들. 아동/로봇의 응답을 기다리는
#: WAIT_CHILD · JUDGE · ROBOT_TURN · ROBOT_VERIFY 는 여기 없다.
AUTO_ADVANCE: dict[Phase, int] = {
    Phase.INTRO: 2500,
    Phase.SHOW_LIST: 2500,
    Phase.REQUEST: 1500,
    Phase.CORRECT: 1500,
    Phase.INCORRECT: 2000,
    Phase.DUPLICATE: 2000,
    Phase.ROBOT_FAIL: 2500,
    Phase.COMPLETE: 3000,
}

TIMER_ADVANCE = "__advance__"


def now_ms() -> int:
    return time.monotonic_ns() // 1_000_000


class Orchestrator:
    def __init__(self, scenario: Scenario, *, log_dir: str = "logs", auto_advance: bool = True,
                 advance_ms: int | None = None):
        self.scenario = scenario
        self.hub = Hub()
        self.state = SessionState()
        self.auto_advance = auto_advance
        #: 모든 자동 advance 지연을 이 값으로 덮는다. 테스트와 데모 템포 조절용.
        self.advance_ms = advance_ms
        self.session_id = f"{scenario.scenario_id}-{uuid.uuid4().hex[:8]}"
        self.log = EpisodeLogger(self.session_id, log_dir)
        self._timers: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    # -------------------------------------------------------------- 이벤트
    async def submit(self, event: ev.InboundEvent) -> dict[str, Any]:
        """모든 이벤트의 단일 진입점. 직렬화해서 전이가 겹치지 않게 한다."""
        async with self._lock:
            before = self.state.phase
            tr = handle(self.state, event, self.scenario, now_ms=now_ms())
            self.state = tr.state
            await self._dispatch(tr.effects)
            if self.state.phase is not before:
                self._schedule_auto_advance(self.state.phase)
            return {"phase": self.state.phase.value,
                    "packed": list(self.state.packed),
                    "remaining": list(self.state.remaining(self.scenario))}

    async def _dispatch(self, effects) -> None:
        for eff in effects:
            match eff:
                case Broadcast():
                    await self.hub.send(eff.roles, eff.payload)
                case SendRobot():
                    await self.hub.send_robot(eff.payload)
                case RequestJudge():
                    await self.hub.send(
                        (ev.Role.VLM, ev.Role.OPERATOR),
                        {"type": "judge_request", "checklist": eff.checklist,
                         "packed": eff.packed, "target": eff.target},
                    )
                case RequestVerify():
                    await self.hub.send(
                        (ev.Role.VLM, ev.Role.OPERATOR),
                        {"type": "verify_request", "cmd_id": eff.cmd_id, "target": eff.target},
                    )
                case SetWatch():
                    # 감시는 capture 가 수행한다. 운영자 콘솔도 같이 받아 현재 모드를 표시한다.
                    await self.hub.send(
                        (ev.Role.CAPTURE, ev.Role.OPERATOR),
                        {"type": "set_watch", "mode": eff.mode},
                    )
                case SetListen():
                    # 마이크 창은 voice 가 연다. 운영자 콘솔도 상태를 같이 본다.
                    await self.hub.send(
                        (ev.Role.VOICE, ev.Role.OPERATOR),
                        {"type": "set_listen", "mode": eff.mode},
                    )
                case Log():
                    self.log.write(eff.event, eff.fields)
                case StartTimer():
                    self._start_timer(eff.name, eff.ms, eff.cmd_id)
                case CancelTimer():
                    self._cancel_timer(eff.name)

    # -------------------------------------------------------------- 타이머
    def _start_timer(self, name: str, ms: int, cmd_id: str | None = None) -> None:
        self._cancel_timer(name)

        async def fire() -> None:
            try:
                await asyncio.sleep(ms / 1000)
            except asyncio.CancelledError:
                return
            self._timers.pop(name, None)
            await self.submit(ev.Timeout(timer=name, cmd_id=cmd_id))

        self._timers[name] = asyncio.create_task(fire())

    def _cancel_timer(self, name: str) -> None:
        task = self._timers.pop(name, None)
        if task and not task.done():
            task.cancel()

    def _schedule_auto_advance(self, phase: Phase) -> None:
        """발화가 끝났다는 신호를 대신 만들어 준다 — **태블릿이 없을 때만.**

        태블릿이 붙어 있으면 그쪽이 실제 음성 재생이 끝난 시점에 `advance` 를 보낸다.
        그때도 타이머를 돌리면 둘이 경쟁한다. 인트로는 녹음 3줄이라 10초쯤 걸리는데
        타이머는 2.5초라, 자동 전진이 **아동이 듣는 도중에 화면을 넘겨 버린다.**
        """
        self._cancel_timer(TIMER_ADVANCE)
        if not self.auto_advance:
            return
        # 발화가 끝났다는 신호를 낼 수 있는 쪽이 붙어 있으면 타이머는 물러난다.
        # 순서가 있다 — **재생을 실제로 한 쪽이 가장 정확하다.**
        #   audio  : 스피커가 소리를 다 낸 시점을 안다
        #   tablet : 브라우저 재생이 끝난 시점. 자동재생 정책·네트워크 지터를 탄다
        #   타이머  : 아무도 없을 때의 대역 (--sim 리허설)
        if self.hub.count(ev.Role.AUDIO) > 0 or self.hub.count(ev.Role.TABLET) > 0:
            return
        delay = AUTO_ADVANCE.get(phase)
        if delay is None:
            return
        if self.advance_ms is not None:
            delay = self.advance_ms

        async def fire() -> None:
            try:
                await asyncio.sleep(delay / 1000)
            except asyncio.CancelledError:
                return
            self._timers.pop(TIMER_ADVANCE, None)
            await self.submit(ev.Advance(from_phase=phase.value))

        self._timers[TIMER_ADVANCE] = asyncio.create_task(fire())

    async def shutdown(self) -> None:
        for name in list(self._timers):
            self._cancel_timer(name)
        self.log.close()

    def current_state_event(self) -> dict[str, Any]:
        """지금 상태를 `state` 이벤트 모양으로. 새로 붙은 스포크를 동기화하는 데 쓴다.

        태블릿은 뷰라서 `state` 를 받아야만 무엇을 그릴지 안다. 접속 직후에 이걸 보내지
        않으면, 다음 전이가 일어날 때까지 빈 화면에 머문다 — 세션 도중 태블릿이
        재연결하면(와이파이 끊김·화면 잠금) 그 구간이 통째로 비어 버린다.
        """
        from orchestrator.machine import _state_event

        return _state_event(self.state, self.scenario)

    def resync(self, peer: ev.Role) -> list[dict[str, Any]]:
        """재연결한 스포크에게 보낼 현재 상태.

        `SetWatch` · `SetListen` 은 모드가 **바뀔 때만** 나간다 (전이마다 명령을
        남발하지 않으려는 설계). 그래서 세션 도중 감시나 음성 서비스가 재시작하면,
        상태 머신은 이미 `judge` 라고 알고 있는데 스포크는 `off` 인 채로 남는다 —
        아동이 물건을 올려도 아무도 보지 않는다. 태블릿도 같은 이유로 빈 화면에 머문다.
        """
        if peer in (ev.Role.TABLET, ev.Role.OPERATOR):
            return [self.current_state_event()]
        if peer is ev.Role.CAPTURE:
            return [{"type": "set_watch", "mode": self.state.watch_mode}]
        if peer is ev.Role.VOICE:
            return [{"type": "set_listen", "mode": self.state.listen_mode}]
        return []

    def health(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "scenario_id": self.scenario.scenario_id,
            "phase": self.state.phase.value,
            "packed": list(self.state.packed),
            "remaining": list(self.state.remaining(self.scenario)),
            "cameras_ok": self.state.cameras_ok,
            "peers": self.hub.health(),
            "log": str(self.log.path),
        }


def create_app(scenario: Scenario, *, log_dir: str = "logs", auto_advance: bool = True,
               advance_ms: int | None = None) -> FastAPI:
    orch = Orchestrator(scenario, log_dir=log_dir, auto_advance=auto_advance, advance_ms=advance_ms)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await orch.shutdown()

    app = FastAPI(title="openarm-specialedu orchestrator", lifespan=lifespan)
    app.state.orchestrator = orch

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return orch.health()

    @app.post("/v1/event")
    async def post_event(payload: dict) -> dict[str, Any]:
        """운영자 콘솔/CLI 용 REST 진입점. WS 와 같은 계약을 쓴다."""
        try:
            event = ev.parse_inbound(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc
        if not ev.sender_allowed(event.type, ev.Role.OPERATOR):
            raise HTTPException(status_code=403, detail=f"operator 는 {event.type} 를 보낼 수 없다")
        return await orch.submit(event)

    @app.websocket("/ws/{role}")
    async def ws(socket: WebSocket, role: str) -> None:
        try:
            peer = ev.Role(role)
        except ValueError:
            await socket.close(code=4004)
            return
        await socket.accept()
        await orch.hub.join(peer, socket)
        await socket.send_json({"type": "welcome", "role": peer.value, **orch.health()})
        # 붙자마자 지금 상태를 한 번 보낸다. 스포크는 명령을 **전이 시점에만** 받으므로,
        # 세션 도중 재연결하면 그 전이를 놓친 채 영원히 잘못된 모드로 남는다.
        for payload in orch.resync(peer):
            await socket.send_json(payload)
        try:
            while True:
                raw = await socket.receive_json()
                try:
                    event = ev.parse_inbound(raw)
                except ValidationError as exc:
                    await socket.send_json({"type": "error", "detail": exc.errors(include_url=False)})
                    continue
                # 역할이 보낼 수 없는 이벤트는 여기서 끊는다.
                if not ev.sender_allowed(event.type, peer):
                    orch.log.write("forbidden_event", {"role": peer.value, "event_type": event.type})
                    await socket.send_json(
                        {"type": "error", "detail": f"{peer.value} 는 {event.type} 를 보낼 수 없다"}
                    )
                    continue
                await orch.submit(event)
        except WebSocketDisconnect:
            pass
        finally:
            await orch.hub.leave(peer, socket)

    return app


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", default="minsu_playdate_v1")
    p.add_argument("--scenario-dir", default="scenarios")
    p.add_argument("--log-dir", default="logs")
    p.add_argument("--host", default=os.environ.get("ORCH_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("ORCH_PORT", 8000)))
    p.add_argument("--no-auto-advance", action="store_true",
                   help="태블릿이 advance 를 보낼 때 쓴다 (2단계 이후)")
    p.add_argument("--advance-ms", type=int, default=None,
                   help="자동 advance 지연을 일괄 덮어쓴다 (템포 조절)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    scenario = find_scenario(args.scenario, args.scenario_dir)
    app = create_app(scenario, log_dir=args.log_dir, auto_advance=not args.no_auto_advance,
                     advance_ms=args.advance_ms)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
