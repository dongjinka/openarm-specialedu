"""로봇 브릿지 — 오케스트레이터의 `robot` 역할 클라이언트.

받는 것: robot_cmd · robot_abort
내는 것: progress_tick · robot_done · robot_error · camera_health

`--sim` 이 기본이다. 실기(`--real`)는 5단계에서 lerobot 버전이 확정된 뒤에 켠다.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import pathlib
import signal

import websockets

import openarm_env

from robot_bridge.backend import RobotBackend
from robot_bridge.preflight import check_state
from robot_bridge.sim import DEFAULT_MS, SimBackend

logger = logging.getLogger(__name__)

CAMERAS = ("follower_d455f", "left_wrist", "right_wrist")


class BridgeClient:
    def __init__(self, url: str, backend: RobotBackend, *, report_cameras: bool = True) -> None:
        self.url = url
        self.backend = backend
        self.report_cameras = report_cameras
        self._task: asyncio.Task | None = None

    async def run(self) -> None:
        async for socket in websockets.connect(self.url, ping_interval=20):
            try:
                logger.info("오케스트레이터 연결됨 (%s, backend=%s)", self.url, self.backend.name)
                if self.report_cameras:
                    # 3대 전부가 정책 필수 입력이다. 한 대만 끊겨도 추론을 막아야 한다.
                    await self._send(socket, {"type": "camera_health",
                                              "cameras": {c: True for c in CAMERAS},
                                              "all_ok": True})
                async for raw in socket:
                    await self._on_message(socket, json.loads(raw))
            except websockets.ConnectionClosed:
                logger.warning("연결 끊김 — 재연결")
                continue

    async def _send(self, socket, payload: dict) -> None:
        await socket.send(json.dumps(payload, ensure_ascii=False))

    async def _on_message(self, socket, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "robot_cmd":
            if self._task and not self._task.done():
                logger.warning("이미 실행 중 — cmd_id=%s 무시", msg.get("cmd_id"))
                return
            self._task = asyncio.create_task(self._run_cmd(socket, msg))
        elif kind == "robot_abort":
            cmd_id = msg.get("cmd_id")
            if cmd_id:
                await self.backend.abort(cmd_id, msg.get("reason", "operator"))

    async def drain(self, timeout: float = 30.0) -> None:
        """실행 중인 `robot_cmd` 가 풀리기를 기다린다.

        종료 정리(teardown)는 `disconnect()` 에서 토크를 끄는데, 그때까지 제어
        스레드가 아직 액션을 보내고 있으면 안 된다. `ThreadSafeRobot` 의 락이
        버스 자체는 지켜 주지만, 순서까지 지켜 주지는 않는다.
        """
        task = self._task
        if task is None or task.done():
            return
        logger.info("실행 중인 동작이 끝나기를 기다린다 (최대 %.0f초)", timeout)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            logger.warning("동작이 제때 풀리지 않았다 — 그대로 정리를 진행한다")
        except Exception:  # noqa: BLE001 — 정리를 막지 않는다
            logger.exception("실행 중이던 동작이 예외로 끝났다")

    async def _run_cmd(self, socket, cmd: dict) -> None:
        cmd_id = cmd["cmd_id"]
        deadline_ms = int(cmd.get("deadline_ms", 69_500))
        target = cmd.get("target")

        # 호출 직전 관절 봉투 검사. 벗어나면 추론하지 않는다 — 클리핑된 state 로
        # 움직이는 것보다 멈추는 편이 안전하다.
        pf = check_state(self.backend.read_state())
        if not pf.ok:
            await self._send(socket, {"type": "robot_error", "cmd_id": cmd_id,
                                      "reason": "preflight", "detail": pf.detail})
            return

        async def on_progress(elapsed_ms: int) -> None:
            await self._send(socket, {"type": "progress_tick", "cmd_id": cmd_id,
                                      "elapsed_ms": elapsed_ms})

        try:
            outcome = await self.backend.execute(cmd_id, target, deadline_ms, on_progress)
        except Exception as exc:  # noqa: BLE001
            logger.exception("실행 실패 cmd_id=%s", cmd_id)
            await self._send(socket, {"type": "robot_error", "cmd_id": cmd_id,
                                      "reason": "hardware_error", "detail": str(exc)})
            return

        await self._send(socket, {"type": "robot_done", "cmd_id": cmd_id,
                                  "success": outcome.success, "reason": outcome.reason,
                                  "duration_ms": outcome.duration_ms})


def install_signal_handlers(backend: RobotBackend, stopping: asyncio.Event) -> None:
    """SIGINT·SIGTERM 을 **우리가** 받는다. `backend.load()` 뒤에 부를 것.

    `ArmSession`(openarm_sciedu)이 `robot.connect()` 안에서 이미 핸들러를 걸어
    두는데, 그 방식은 신호를 플래그로만 받아 두고 `raise_if_stopping()` 이
    다음 제어 틱에 예외로 바꾸는 구조다. lerobot 의 맨 `while True:` 루프를
    빠져나오게 하려고 만든 것이라, 우리 구조에서는 두 가지 이유로 동작하지 않는다.

      · 그 함수를 부르는 곳은 `get_observation()` 과 `send_action()` 둘뿐이다.
        턴과 턴 사이(아이에게 다음 물건을 말하는 구간)에는 아무도 부르지 않아서
        종료 신호가 대기열에 걸린 채 남는다 — 실제로 SIGTERM 을 두 번 보내고도
        프로세스가 계속 살아 있는 것을 로그에서 확인했다.
      · 프레임 서버로 그 경로를 깨워도 소용이 없다. 예외가 uvicorn 의
        `except BaseException` 에 잡혀 500 으로 삼켜지는데, `raise_if_stopping()`
        은 **한 번만** 던지므로 그 한 번을 거기서 소모해 버린다.

    그래서 우리가 가져온다. 우리는 롤아웃을 `shutdown` 이벤트로 세우므로 플래그를
    예외로 바꿀 필요가 없고, `ArmSession._stopping` 이 False 로 남는 편이 오히려
    안전하다 — 시작 자세로 되돌아가는 중(`return_to_initial_position` 이
    `get_observation()` 을 부른다)에 그 일회성 예외가 튀지 않는다.

    되돌아가는 경로 자체는 이미 있는 것을 그대로 쓴다. `backend.close()` 가
    `strategy.teardown()` → `return_to_initial_position`(약 3초, 부드러운 보간)
    → `robot.disconnect()` → `ArmSession.shutdown()` → `descend_to_rest`
    (`lower_seconds` 기본 4초, 45°/s 제한) → `disable_torque()` 순으로 돈다.
    **정자세로 천천히 돌아온 뒤에 토크를 푸는 것이 이 순서다.**
    """
    loop = asyncio.get_running_loop()
    seen = 0

    def on_signal(signum: int) -> None:
        nonlocal seen
        seen += 1
        if seen > 1:
            # 두 번째 신호로 정리를 끊지 않는다. `GrootBackend.close()` 는
            # teardown 을 워커 스레드에서 돌리는데, 거기서는 `ArmSession.shutdown()`
            # 의 자체 보호(`signal.signal(SIG_IGN)`)가 "메인 스레드가 아니다" 로
            # 건너뛰어진다 — 즉 지금 이 핸들러가 하강을 지키는 유일한 장치다.
            # 내려오는 도중에 끊기면 팔이 공중에서 토크를 잃는다.
            logger.warning("신호 %s — 이미 정리 중이다. 무시한다 "
                           "(내려오는 중에 끊으면 팔이 공중에서 힘을 잃는다). "
                           "정말 급하면 비상정지 버튼을 쓸 것.", signum)
            return
        logger.warning("신호 %s — 정지한다. 시작 자세로 되돌아간 뒤 내려놓고 토크를 푼다.",
                       signum)
        ev = getattr(backend, "shutdown", None)
        if ev is not None:
            ev.set()            # 돌고 있던 롤아웃이 다음 틱에 빠져나온다
        stopping.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, on_signal, sig)
        except (NotImplementedError, RuntimeError):     # 윈도우·비메인스레드
            logger.warning("신호 %s 를 가로채지 못했다", sig)


def load_robot_config(path: str):
    """로봇 설정 JSON → lerobot RobotConfig 인스턴스.

    `{"type": "bi_openarm_follower", ...}` 형태다. `type` 은 draccus ChoiceRegistry
    이름이고 나머지는 그 설정 클래스의 필드다 (포트·카메라 3대 등).
    데이터셋 `meta/info.json` 의 `robot_type` 과 **같아야** 한다.
    """
    import json

    from lerobot.robots.config import RobotConfig
    from lerobot.utils.import_utils import register_third_party_plugins

    register_third_party_plugins()          # 플러그인 로봇을 레지스트리에 올린다
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    kind = data.pop("type", None)
    if not kind:
        raise ValueError(f"{path}: 'type' 필드가 필요하다 (예: bi_openarm_follower)")
    return RobotConfig.get_choice_class(kind)(**data)


def build_backend(args) -> RobotBackend:
    if args.real:
        from robot_bridge.groot_runner import GrootBackend

        if not args.policy_path:
            raise SystemExit(
                "--real 에는 --policy-path 가 필요하다. 60에피소드 재학습 체크포인트를 가리켜야 한다 "
                "— 옛 120epi 정책은 태스크 문자열과 관절 봉투가 모두 다르다."
            )
        if not args.robot_config:
            raise SystemExit("--real 에는 --robot-config 가 필요하다 (로봇·카메라 3대 설정 JSON)")
        return GrootBackend(args.policy_path, args.device,
                            robot_cfg=load_robot_config(args.robot_config), fps=args.fps)
    return SimBackend(frames_dir=args.frames_dir, duration_ms=args.sim_ms,
                      realistic=args.realistic, fail_rate=args.fail_rate, seed=args.seed)


def main() -> None:
    p = argparse.ArgumentParser()
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--sim", action="store_true", default=True, help="하드웨어 없이 계약만 대역 (기본)")
    mode.add_argument("--real", action="store_true", help="실제 GR00T 추론 (5단계)")
    p.add_argument("--url", default="ws://127.0.0.1:8000/ws/robot")
    p.add_argument("--sim-ms", type=int, default=DEFAULT_MS)
    p.add_argument("--realistic", action="store_true",
                   help="실측 분포(평균 46.3초)로 돌린다 — 리허설용")
    p.add_argument("--fail-rate", type=float, default=0.0, help="시뮬레이션 실패 주입 비율")
    p.add_argument("--seed", type=int, default=None)
    # 옛 120epi 정책을 기본값으로 두면 조용히 잘못된 봉투·태스크 문자열로 돌게 된다.
    # 기본값을 없애 --real 이 명시를 강제하도록 했다.
    p.add_argument("--policy-path", default=None,
                   help="60epi 재학습 체크포인트 경로 또는 repo id (--real 필수)")
    p.add_argument("--robot-config", default=None,
                   help="로봇·카메라 3대 설정 JSON 경로 (--real 필수)")
    p.add_argument("--fps", type=float, default=30.0, help="제어 루프 목표 레이트")
    p.add_argument("--device", default="cuda")
    p.add_argument("--frames-dir", default=None,
                   help="--sim 에서 /frame/latest 로 내보낼 이미지 디렉터리 (전 구간 리허설용)")
    p.add_argument("--frame-port", type=int, default=8081,
                   help="씬 카메라 배포 포트. 0 이면 서버를 띄우지 않는다")
    p.add_argument("--frame-host", default="127.0.0.1")
    args = p.parse_args()

    openarm_env.load()      # .env — ORCH_HOST · API 키
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    backend = build_backend(args)
    client = BridgeClient(args.url, backend)

    async def boot() -> None:
        if hasattr(backend, "load"):
            await backend.load()      # 시작 시 1회 로드해 상주 (§6)

        # load() 안의 robot.connect() 가 ArmSession 핸들러를 걸어 둔 **뒤에** 가져온다.
        stopping = asyncio.Event()
        install_signal_handlers(backend, stopping)

        jobs = [asyncio.create_task(client.run(), name="bridge")]
        if args.frame_port:
            # 카메라를 여는 프로세스는 여기 하나뿐이다. VLM 은 HTTP 로 1장씩 받아 간다.
            from robot_bridge.frame_server import serve

            logger.info("프레임 배포 http://%s:%d/frame/latest", args.frame_host, args.frame_port)
            jobs.append(asyncio.create_task(
                serve(backend, args.frame_host, args.frame_port), name="frames"))
        waiter = asyncio.create_task(stopping.wait(), name="stop")
        try:
            await asyncio.wait([*jobs, waiter], return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (*jobs, waiter):
                task.cancel()
            # 취소를 스스로 처리할 시간을 준다. 그냥 두고 넘어가면 웹소켓·uvicorn 이
            # 정리 도중에 트레이스백을 뿜어 **정작 봐야 할 teardown 로그를 묻는다.**
            await asyncio.gather(*jobs, waiter, return_exceptions=True)
            # 돌고 있던 동작이 먼저 풀려야 한다 — 아래 teardown 이 토크를 끈다.
            await client.drain()
            # teardown 은 시작 자세 복귀 + 하강 + 토크 해제 + disconnect 다.
            # 프로세스 종료 시 1회뿐 — 턴 사이에 부르면 안 된다.
            if hasattr(backend, "close"):
                await backend.close()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(boot())


if __name__ == "__main__":
    main()
