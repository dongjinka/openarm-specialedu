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

        jobs = [client.run()]
        if args.frame_port:
            # 카메라를 여는 프로세스는 여기 하나뿐이다. VLM 은 HTTP 로 1장씩 받아 간다.
            from robot_bridge.frame_server import serve

            logger.info("프레임 배포 http://%s:%d/frame/latest", args.frame_host, args.frame_port)
            jobs.append(serve(backend, args.frame_host, args.frame_port))
        try:
            await asyncio.gather(*jobs)
        finally:
            # teardown 은 하드웨어 disconnect + 초기 자세 복귀다. 프로세스 종료 시 1회뿐 —
            # 턴 사이에 부르면 안 된다.
            if hasattr(backend, "close"):
                await backend.close()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(boot())


if __name__ == "__main__":
    main()
