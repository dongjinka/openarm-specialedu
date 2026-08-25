"""end-to-end — 진짜 서버 + 진짜 브릿지 코드, WS 로만 통신.

로봇도 카메라도 VLM 도 없이 물건 3개 세션이 끝까지 돈다.
검증의 핵심은 마지막의 JSONL 로그 — §7 이 요구한 평가 근거 데이터가 실제로 남는지.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket

import pytest
import uvicorn
import websockets

from orchestrator.main import create_app
from orchestrator.scenario import find_scenario
from robot_bridge.main import BridgeClient
from robot_bridge.sim import SimBackend

SC = find_scenario("minsu_playdate_v1")
TIMEOUT = 60


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Harness:
    def __init__(self, tmp_path, **app_kw):
        self.port = free_port()
        self.app = create_app(SC, log_dir=str(tmp_path), advance_ms=60, **app_kw)
        self.orch = self.app.state.orchestrator
        self._tasks: list[asyncio.Task] = []

    def url(self, role: str) -> str:
        return f"ws://127.0.0.1:{self.port}/ws/{role}"

    async def __aenter__(self):
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self._tasks.append(asyncio.create_task(self.server.serve()))
        for _ in range(200):
            if getattr(self.server, "started", False):
                break
            await asyncio.sleep(0.02)
        assert self.server.started, "서버가 뜨지 않았다"
        return self

    async def __aexit__(self, *exc):
        self.server.should_exit = True
        for t in self._tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        return task

    def start_robot(self, **kw) -> asyncio.Task:
        backend = SimBackend(duration_ms=kw.pop("duration_ms", 250), seed=7, **kw)
        return self.spawn(BridgeClient(self.url("robot"), backend).run())


async def send(ws, payload: dict) -> None:
    await ws.send(json.dumps(payload, ensure_ascii=False))


async def run_session(h: Harness, *, verify_ok=lambda obj: (True, True)) -> list[dict]:
    """운영자·VLM·태블릿을 붙이고 세션을 끝까지 몬다. 태블릿이 본 state 목록을 준다."""
    seen: list[dict] = []
    done = asyncio.Event()

    async with (
        websockets.connect(h.url("operator")) as op,
        websockets.connect(h.url("vlm")) as vlm,
        websockets.connect(h.url("tablet")) as tab,
    ):
        # 발화가 끝났음을 알리는 상태들. 실제 태블릿은 녹음 재생이 끝난 시점에 낸다.
        SPEAKS = {"INTRO", "SHOW_LIST", "REQUEST", "CORRECT", "INCORRECT",
                  "DUPLICATE", "ROBOT_FAIL", "COMPLETE"}

        async def tablet_loop():
            async for raw in tab:
                msg = json.loads(raw)
                if msg.get("type") == "state":
                    seen.append(msg)
                    if msg["phase"] == "END":
                        done.set()
                    elif msg["phase"] in SPEAKS:
                        # 태블릿이 붙어 있으면 오케스트레이터는 자동 전진을 하지 않는다.
                        # 진행은 "발화가 끝났다"는 이 신호로만 일어난다.
                        await send(tab, {"type": "advance", "from_phase": msg["phase"]})

        async def operator_loop():
            async for raw in op:
                msg = json.loads(raw)
                if msg.get("type") == "state" and msg["phase"] == "WAIT_CHILD":
                    await send(op, {"type": "child_placed"})
                elif msg.get("type") == "verify_request":
                    in_bag, closed = verify_ok(msg.get("target"))
                    await send(op, {"type": "verify_result", "cmd_id": msg["cmd_id"],
                                    "object_in_bag": in_bag, "bag_closed": closed})

        async def vlm_loop():
            async for raw in vlm:
                msg = json.loads(raw)
                if msg.get("type") == "judge_request" and msg.get("target"):
                    await send(vlm, {"type": "judge", "object": msg["target"],
                                     "should_pack": True, "confidence": 0.95})

        loops = [h.spawn(tablet_loop()), h.spawn(operator_loop()), h.spawn(vlm_loop())]
        h.start_robot()
        await asyncio.sleep(0.2)
        await send(op, {"type": "session_start", "scenario_id": SC.scenario_id})
        await asyncio.wait_for(done.wait(), timeout=TIMEOUT)
        for t in loops:
            t.cancel()
    return seen


@pytest.mark.asyncio
async def test_full_session_end_to_end(tmp_path):
    async with Harness(tmp_path) as h:
        seen = await run_session(h)

        assert h.orch.state.phase.value == "END"
        assert set(h.orch.state.packed) == {"flower", "whale", "tree"}

        phases = [s["phase"] for s in seen]
        assert "BAG_SETUP" not in phases, "가방 세팅은 아동 과제가 아니다"
        for expected in ("INTRO", "SHOW_LIST", "REQUEST", "WAIT_CHILD", "JUDGE",
                         "CORRECT", "ROBOT_TURN", "ROBOT_VERIFY", "COMPLETE", "END"):
            assert expected in phases, expected

        # JUDGE 는 thinking 을 즉시 띄워 판정 지연의 공백을 메운다 (§4.5)
        judge = next(s for s in seen if s["phase"] == "JUDGE")
        assert judge["expression"] == "thinking"
        # 로봇 동작 중에는 발화하지 않는다 — 아동 시선이 로봇 팔로 가야 한다
        for s in seen:
            if s["phase"] == "ROBOT_TURN":
                assert s["utterance_id"] is None

        # ---- §7 평가 근거 데이터 ------------------------------------
        records = h.orch.log.read_all()
        assert all(r["seq"] == i + 1 for i, r in enumerate(records)), "seq 가 단조가 아니다"

        responses = [r for r in records if r["event"] == "child_response"]
        assert len(responses) == 3
        for r in responses:
            for field in ("correct", "latency_ms", "retry", "prompt_level", "independent",
                          "on_target", "object", "requested"):
                assert field in r, f"§7 필수 필드 누락: {field}"

        cmds = [r for r in records if r["event"] == "robot_cmd"]
        dones = [r for r in records if r["event"] == "robot_done"]
        assert [c["cmd_id"] for c in cmds] == ["c1", "c2", "c3"]
        assert len(dones) == 3 and all(d["success"] for d in dones)
        assert all("duration_ms" in d for d in dones)

        verifies = [r for r in records if r["event"] == "robot_verify"]
        assert len(verifies) == 3
        assert all(v["object_in_bag"] and v["bag_closed"] for v in verifies)


@pytest.mark.asyncio
async def test_verify_failure_drops_into_robot_fail(tmp_path):
    """사후 확인이 실패하면 로봇의 실수로 안내되고, 아동은 실패로 끝나지 않는다."""
    first = {"n": 0}

    def verdict(_target):
        first["n"] += 1
        return (False, True) if first["n"] == 1 else (True, True)

    async with Harness(tmp_path) as h:
        seen = await run_session(h, verify_ok=verdict)

        phases = [s["phase"] for s in seen]
        assert "ROBOT_FAIL" in phases
        fail = next(s for s in seen if s["phase"] == "ROBOT_FAIL")
        assert fail["utterance_id"] == "robot_mistake"

        # 실패한 물건은 되돌려졌다가 다시 요청된다 → 세션은 그래도 완주한다
        assert h.orch.state.phase.value == "END"
        records = h.orch.log.read_all()
        verifies = [r for r in records if r["event"] == "robot_verify"]
        assert verifies[0]["success"] is False
        assert sum(1 for r in records if r["event"] == "robot_cmd") >= 4


@pytest.mark.asyncio
async def test_role_gating_blocks_vlm_from_moving_the_arm(tmp_path):
    """§12 — VLM 이 로봇 이벤트를 보낼 경로는 없다. 서버가 거부한다."""
    async with Harness(tmp_path) as h:
        async with websockets.connect(h.url("vlm")) as vlm:
            await vlm.recv()  # welcome
            await send(vlm, {"type": "robot_done", "cmd_id": "c1", "success": True,
                             "reason": "verified", "duration_ms": 1})
            reply = json.loads(await asyncio.wait_for(vlm.recv(), timeout=5))
            assert reply["type"] == "error"
            assert "robot_done" in reply["detail"]

        records = h.orch.log.read_all()
        assert any(r["event"] == "forbidden_event" for r in records)


@pytest.mark.asyncio
async def test_camera_loss_prevents_the_arm_from_moving(tmp_path):
    """카메라 3대 전부가 정책 필수 입력이다 — 한 대만 죽어도 추론하지 않는다."""
    async with Harness(tmp_path) as h:
        reached_fail = asyncio.Event()
        async with (
            websockets.connect(h.url("operator")) as op,
            websockets.connect(h.url("vlm")) as vlm,
            websockets.connect(h.url("capture")) as cap,
        ):
            async def op_loop():
                async for raw in op:
                    msg = json.loads(raw)
                    if msg.get("type") == "state":
                        if msg["phase"] == "WAIT_CHILD":
                            await send(op, {"type": "child_placed"})
                        elif msg["phase"] == "ROBOT_FAIL":
                            reached_fail.set()

            async def vlm_loop():
                async for raw in vlm:
                    msg = json.loads(raw)
                    if msg.get("type") == "judge_request" and msg.get("target"):
                        await send(vlm, {"type": "judge", "object": msg["target"],
                                         "should_pack": True, "confidence": 0.95})

            h.spawn(op_loop())
            h.spawn(vlm_loop())
            h.start_robot()
            await asyncio.sleep(0.2)
            await send(cap, {"type": "camera_health",
                             "cameras": {"follower_d455f": True, "left_wrist": False,
                                         "right_wrist": True},
                             "all_ok": False})
            await asyncio.sleep(0.1)
            await send(op, {"type": "session_start", "scenario_id": SC.scenario_id})
            await asyncio.wait_for(reached_fail.wait(), timeout=TIMEOUT)

        records = h.orch.log.read_all()
        assert any(r["event"] == "robot_blocked" for r in records)
        assert not any(r["event"] == "robot_cmd" for r in records), "팔이 움직이면 안 된다"


async def test_auto_advance_yields_to_a_connected_tablet(tmp_path):
    """태블릿이 붙어 있으면 오케스트레이터는 스스로 전진하지 않는다.

    자동 전진은 태블릿이 없을 때 `--sim` 만으로 전 구간을 돌리기 위한 대역이다.
    실제 태블릿이 붙은 뒤에도 타이머가 돌면, 인트로 녹음 3줄(≈10초)이 끝나기 전에
    화면이 넘어가 **아동이 듣던 중에 장면이 바뀐다.**
    """
    async with Harness(tmp_path) as h:
        async with websockets.connect(h.url("operator")) as op:
            async with websockets.connect(h.url("tablet")) as tab:
                phases: list[str] = []

                async def watch():
                    async for raw in tab:
                        msg = json.loads(raw)
                        if msg.get("type") == "state":
                            phases.append(msg["phase"])

                task = h.spawn(watch())
                await asyncio.sleep(0.2)
                await send(op, {"type": "session_start", "scenario_id": SC.scenario_id})
                # advance_ms=60 이므로 자동 전진이 살아 있었다면 이 사이에 여러 번 넘어간다.
                await asyncio.sleep(0.8)
                task.cancel()

                assert phases, "state 를 하나도 못 받았다"
                assert phases[-1] == "INTRO", f"태블릿을 기다리지 않고 전진했다: {phases}"

                # 태블릿이 발화 종료를 알리면 그때 넘어간다.
                await send(tab, {"type": "advance", "from_phase": "INTRO"})
                for _ in range(50):
                    await asyncio.sleep(0.02)
                    if h.orch.state.phase.value != "INTRO":
                        break
                assert h.orch.state.phase.value == "SHOW_LIST"


async def test_auto_advance_still_runs_without_a_tablet(tmp_path):
    """태블릿이 없으면 대역이 살아 있어야 한다 — `--sim` 리허설이 여기에 기댄다."""
    async with Harness(tmp_path) as h:
        async with websockets.connect(h.url("operator")) as op:
            await asyncio.sleep(0.2)
            await send(op, {"type": "session_start", "scenario_id": SC.scenario_id})
            for _ in range(100):
                await asyncio.sleep(0.02)
                if h.orch.state.phase.value not in ("IDLE", "INTRO"):
                    break
            assert h.orch.state.phase.value != "INTRO", "태블릿이 없는데 전진하지 않았다"


async def test_reconnecting_spokes_are_resynced(tmp_path):
    """재연결한 스포크는 지금 모드를 다시 받아야 한다.

    `SetWatch` · `SetListen` 은 모드가 **바뀔 때만** 나간다. 세션 도중 감시나 음성
    서비스가 재시작하면, 상태 머신은 이미 `judge` 라고 알고 있는데 스포크는 `off` 인
    채로 남는다 — 아동이 물건을 올려도 아무도 보지 않는다.
    """
    async with Harness(tmp_path) as h:
        async with websockets.connect(h.url("operator")) as op:
            await asyncio.sleep(0.2)
            await send(op, {"type": "session_start", "scenario_id": SC.scenario_id})
            for _ in range(200):
                await asyncio.sleep(0.02)
                if h.orch.state.phase.value == "WAIT_CHILD":
                    break
            assert h.orch.state.phase.value == "WAIT_CHILD"

            # 이제서야 붙는다 = 세션 도중 재시작한 스포크.
            async def greeting(role: str) -> list[dict]:
                """접속 직후 오는 것을 전부 모은다. 역할마다 개수가 다르다."""
                async with websockets.connect(h.url(role)) as ws:
                    seen: list[dict] = []
                    with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                        while True:
                            seen.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=0.4)))
                    return seen

            capture = await greeting("capture")
            assert any(m.get("type") == "set_watch" and m.get("mode") == "judge"
                       for m in capture), f"감시 모드를 못 받았다: {capture}"

            voice = await greeting("voice")
            assert any(m.get("type") == "set_listen" and m.get("mode") == "open"
                       for m in voice), f"마이크 모드를 못 받았다: {voice}"

            tablet = await greeting("tablet")
            assert any(m.get("type") == "state" and m.get("phase") == "WAIT_CHILD"
                       for m in tablet), f"현재 상태를 못 받았다: {tablet}"
