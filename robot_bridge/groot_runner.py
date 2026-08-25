"""GR00T 추론 백엔드 — `lerobot.rollout` 배포 엔진을 감싼다.

**추론 루프를 직접 짜지 않는다.** `openarmsciedu-vla`(LeRobot 포크)의
`src/lerobot/rollout/` 에 이미 완성돼 있다 — sync/rtc 백엔드, `ThreadSafeRobot`,
전략 5종, 워밍업, 액션 보간까지.

CLI(`lerobot-rollout`)가 아니라 **라이브러리로** 부른다. 서브프로세스로 띄우면
물건마다 GR00T 3B 를 로드하게 되고(§12 안티패턴), `teardown()` 이 하드웨어
disconnect 와 `return_to_initial_position` 을 하므로 턴 사이에 부를 수 없다.

    기동 시 1회   build_rollout_context() → strategy.setup()      (모델 상주)
    robot_cmd 마다 shutdown.clear() → cfg.duration → strategy.run()
    중단          shutdown.set()      ← robot_abort · pause · camera all_ok=false
    종료 시 1회   strategy.teardown()

`shutdown_event` 를 **우리가 만들어 넘긴다**(`build_rollout_context(cfg, shutdown)`).
그래서 중단이 이벤트 하나로 끝난다.

lerobot import 는 전부 지연시킨다 — 이 모듈이 import 만으로 `--sim` 경로를 깨면 안 된다.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import threading
import time
from typing import Any

from robot_bridge.backend import ProgressCb, RobotOutcome
from robot_bridge.preflight import GROOT_TASK, STATE_ENVELOPE, check_state

logger = logging.getLogger(__name__)

#: observation.state 16차원의 키 이름을 봉투 정의에서 그대로 가져온다.
#: 두 곳에 순서를 적어두면 반드시 어긋난다.
STATE_KEYS: tuple[str, ...] = tuple(name for name, _, _ in STATE_ENVELOPE)

#: progress_tick 최소 간격. 제어 루프는 초당 수십 회 돌지만 태블릿에는 1초면 충분하다.
PROGRESS_INTERVAL_S = 1.0
#: 봉투 검사 최소 간격.
#:
#: 매 틱(최대 30Hz) 검사하면 `observation.state` 가 GPU 텐서일 때 `.tolist()` 가
#: 매번 동기화를 강제해 제어 루프를 늦춘다. 루프가 늦어지면 데드라인이 늘어나고,
#: 그게 지금 가장 아픈 문제다. 진입 직전 검사(main.py)가 시작 자세를 이미 보므로
#: 여기는 **동작 중 이탈**을 잡는 그물이고, 5Hz 면 충분하다.
ENVELOPE_INTERVAL_S = 0.2


def _extract_state(obs: dict[str, Any]) -> list[float] | None:
    """관측 dict 에서 16차원 관절 상태를 뽑는다. 못 뽑으면 None.

    전처리 전(raw)에는 `left_joint_1.pos` 같은 평평한 키가 오고, 전처리 후에는
    `observation.state` 하나로 묶여 있을 수 있다. 둘 다 받는다 — 키 이름이 어긋나면
    봉투 검사가 **에러 없이 조용히 사라지기** 때문에 여기서 확실히 갈라야 한다.
    """
    if all(k in obs for k in STATE_KEYS):
        return [float(obs[k]) for k in STATE_KEYS]

    packed = obs.get("observation.state")
    if packed is None:
        return None
    values = packed.tolist() if hasattr(packed, "tolist") else list(packed)
    while isinstance(values, list) and len(values) == 1 and isinstance(values[0], list):
        values = values[0]          # 배치 차원 제거
    if len(values) != len(STATE_KEYS):
        return None
    return [float(v) for v in values]


def _declared_policy_type(policy_path: str) -> str | None:
    """체크포인트가 스스로 선언한 정책 타입. 로컬 경로든 HF repo id 든 읽는다."""
    import json

    local = pathlib.Path(policy_path) / "config.json"
    if local.is_file():
        return json.loads(local.read_text(encoding="utf-8")).get("type")
    try:
        from huggingface_hub import hf_hub_download

        path = hf_hub_download(policy_path, "config.json")
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8")).get("type")
    except Exception:  # noqa: BLE001
        return None


def _known_policy_types() -> set[str]:
    try:
        from lerobot.configs import PreTrainedConfig
    except ImportError:
        return set()        # lerobot 이 없으면 검사할 수 없다 — 통과시킨다

    for attr in ("CHOICE_REGISTRY", "_choice_registry", "choice_registry"):
        reg = getattr(PreTrainedConfig, attr, None)
        if isinstance(reg, dict):
            return set(reg)
    getter = getattr(PreTrainedConfig, "get_known_choices", None)
    if callable(getter):
        try:
            return set(getter())
        except Exception:  # noqa: BLE001
            pass
    return set()


def check_policy_type(policy_path: str) -> None:
    """정책을 로드하기 전에 타입 이름이 등록돼 있는지 본다.

    이 체크포인트의 `config.json` 은 `type: groot_frozen_bf16` 이고, 그 설정 클래스는
    **lerobot 이 아니라 openarm_sciedu 플러그인**에 있다:

        openarm_sciedu/policies/groot_frozen_bf16/configuration_groot_frozen_bf16.py
            @PreTrainedConfig.register_subclass("groot_frozen_bf16")

    등록은 `register_third_party_plugins()` 가 `lerobot_robot_openarm_sciedu` 배포판을
    import 하면서 일어난다 — import 하는 것이 메커니즘의 전부다. 그래서 `_load_blocking`
    이 그걸 먼저 부른다.

    이 검사가 걸린다면 그 플러그인이 설치되지 않았다는 뜻이다. 미리 잡지 않으면
    draccus 가 `KeyError: 'groot_frozen_bf16'` 만 던져 원인이 안 보인다.
    """
    declared = _declared_policy_type(policy_path)
    if declared is None:
        logger.warning("체크포인트의 정책 타입을 읽지 못했다 — 로드해 보고 판단한다")
        return
    known = _known_policy_types()
    if not known or declared in known:
        logger.info("정책 타입 %s 등록 확인", declared)
        return
    raise RuntimeError(
        f"정책 타입 '{declared}' 가 등록돼 있지 않다 (등록된 것: {sorted(known)}).\n"
        "이 타입은 lerobot 이 아니라 openarm_sciedu 플러그인이 등록한다:\n"
        "  openarm_sciedu/policies/groot_frozen_bf16/configuration_groot_frozen_bf16.py\n"
        "  → lerobot_robot_openarm_sciedu/__init__.py 가 import 하고,\n"
        "    register_third_party_plugins() 가 그 배포판을 import 하면서 등록된다.\n"
        "→ 그 플러그인이 **이미 설치된** 환경(leap 머신)에서 실행한다.\n"
        "   그 리포의 파일은 수정하지 않는다 — docs/GROOT_POLICY_DEPENDENCY.md 참조.\n"
        "   config.json 의 type 만 'groot' 로 바꾸는 우회는 통하지 않는다 — "
        "frozen_params_dtype · use_peft · pretrained_revision 필드에서 다시 막힌다."
    )


def _build_strategy_class():
    """`BaseStrategy` 서브클래스를 지연 생성한다 (lerobot import 를 미루기 위해)."""
    from lerobot.rollout.strategies.base import BaseStrategy

    class OpenArmStrategy(BaseStrategy):
        """BaseStrategy + 매 틱 안전 게이트.

        `run()` 이 제어 루프를 소유하므로 예전처럼 루프 안에서 `check_state()` 를
        부를 수 없다. 그런데 `_log_telemetry` 가 매 틱 호출되므로 여기가 훅이 된다.
        core.py 에서는 @staticmethod 지만 `self._log_telemetry(a, b, c)` 로 불리므로
        인스턴스 메서드로 오버라이드하면 그대로 잡힌다.
        """

        def bind(self, loop, on_progress: ProgressCb, on_violation) -> None:
            self._loop = loop
            self._on_progress = on_progress
            self._on_violation = on_violation
            self._t0 = time.monotonic()
            self._last_tick = -PROGRESS_INTERVAL_S
            self._last_check = -ENVELOPE_INTERVAL_S
            self._state_seen = False
            self._violated = False
            self._bound = True

        _bound = False

        def _log_telemetry(self, obs_processed, action_dict, runtime_ctx):
            BaseStrategy._log_telemetry(obs_processed, action_dict, runtime_ctx)
            if not self._bound:
                # bind() 없이 불렸다. 여기서 AttributeError 를 내면 동작 중인 팔의
                # 제어 루프가 죽는다 — 조용히 넘기고 텔레메트리만 포기한다.
                return
            elapsed = time.monotonic() - self._t0

            # ① 관절 봉투 — 전처리기가 clip_outliers 로 조용히 뭉개기 전에 잡는다.
            due = elapsed - self._last_check >= ENVELOPE_INTERVAL_S
            state = _extract_state(obs_processed) if (due and obs_processed) else None
            if due:
                self._last_check = elapsed
            if state is None:
                if not self._state_seen:
                    self._state_seen = True
                    logger.error(
                        "관측에서 16차원 관절 상태를 못 찾았다 — **봉투 검사가 꺼진 채 돈다.** "
                        "키: %s", sorted(obs_processed or {})[:12],
                    )
            else:
                if not self._state_seen:
                    self._state_seen = True
                    logger.info("봉투 검사 활성 (관절 16개 확인)")
                pf = check_state(state)
                if not pf.ok and not self._violated:
                    self._violated = True
                    logger.error("관절 봉투 위반 — 즉시 정지: %s", pf.detail)
                    runtime_ctx.shutdown_event.set()
                    self._loop.call_soon_threadsafe(self._on_violation, pf.detail)
                    return

            # ② progress_tick — 무발화 구간의 유일한 진행 신호.
            if elapsed - self._last_tick >= PROGRESS_INTERVAL_S:
                self._last_tick = elapsed
                # 제어 스레드에서 이벤트 루프로 넘긴다. 여기서는 await 할 수 없다.
                asyncio.run_coroutine_threadsafe(
                    self._on_progress(int(elapsed * 1000)), self._loop
                )

    return OpenArmStrategy


class GrootBackend:
    """실제 GR00T 정책. `RobotBackend` 프로토콜을 그대로 만족한다."""

    name = "groot"

    def __init__(self, policy_path: str, device: str = "cuda", *, robot_cfg=None,
                 fps: float = 30.0) -> None:
        self.policy_path = policy_path
        self.device = device
        self.robot_cfg = robot_cfg
        self.fps = fps
        #: 우리가 소유한다 — abort · pause · camera_health 가 지나가는 유일한 통로.
        self.shutdown = threading.Event()
        self.ctx: Any = None
        self.strategy: Any = None
        self.error: str | None = None
        self._aborted: set[str] = set()
        self._lock = asyncio.Lock()

    # ── 기동 시 1회. 모델은 상주한다 ────────────────────────────────────────
    async def load(self) -> None:
        """**메인 스레드에서** 돈다. 워커 스레드로 넘기면 안 된다.

        `build_rollout_context` 가 `robot.connect()` 를 부르고, 그 안의 `ArmSession`
        이 SIGTERM 핸들러를 등록한다(`arm_session.py`). 파이썬은 메인 스레드가 아니면
        `signal.signal` 을 거부하므로 `asyncio.to_thread` 로 감싸면
        `ValueError: signal only works in main thread` 로 죽는다.

        그 핸들러는 장식이 아니다 — `finally` 블록이 잡지 못하는 SIGTERM 에서도 팔을
        내리고 토크를 푸는 유일한 경로다. 우회하지 말고 메인 스레드를 내준다.

        기동 시 1회뿐이고 이때는 아직 다른 일이 없으므로, 30초 남짓 이벤트 루프가
        멈추는 것은 감수한다.
        """
        async with self._lock:
            if self.strategy is not None:
                return
            try:
                self._load_blocking()
            except Exception as exc:  # noqa: BLE001
                self.error = f"{type(exc).__name__}: {exc}"
                logger.exception("GR00T 롤아웃 컨텍스트 구성 실패")

    def _load_blocking(self) -> None:
        from lerobot.configs import PreTrainedConfig
        from lerobot.rollout import RolloutConfig, build_rollout_context
        from lerobot.rollout.configs import BaseStrategyConfig
        from lerobot.rollout.inference import SyncInferenceConfig
        from lerobot.utils.import_utils import register_third_party_plugins

        register_third_party_plugins()      # openarm_sciedu 로봇/텔레옵 등록
        check_policy_type(self.policy_path)  # 알아보기 힘든 KeyError 를 앞에서 막는다

        if self.robot_cfg is None:
            raise RuntimeError(
                "robot_cfg 가 필요하다. 데이터셋 robot_type 은 bi_openarm_follower 이고 "
                "카메라 3대(follower_d455f · left_wrist · right_wrist) 설정을 포함해야 한다."
            )

        # CLI 는 __post_init__ 이 --policy.path 로 채운다. 라이브러리 호출은
        # parser.wrap() 을 안 거치므로 직접 넣는다.
        policy_cfg = PreTrainedConfig.from_pretrained(self.policy_path)
        # 체크포인트 config.json 의 `pretrained_path` 는 None 이고 from_pretrained 도
        # 채우지 않는다. 그런데 context._load_pretrained_policy 가 **이 필드로**
        # 가중치를 찾는다 — 비워 두면 transformers 가 경로 None 으로 허브를 뒤지다가
        # "None is not a local folder" 로 죽는다. 정책 로드 단계라 하드웨어는 아직
        # 붙기 전이다(정책 → 전처리기 → 하드웨어 순).
        policy_cfg.pretrained_path = self.policy_path

        cfg = RolloutConfig(
            robot=self.robot_cfg,
            policy=policy_cfg,
            strategy=BaseStrategyConfig(),      # 녹화 없는 자율 롤아웃
            inference=SyncInferenceConfig(),    # ← sync. RTC 금지: 겹침보정이 그리퍼
                                                #    릴리스 8프레임을 삼킨다 (history/035)
            task=GROOT_TASK,                    # 정책 언어 입력. 바이트 단위 그대로
            fps=self.fps,
            duration=0.0,                       # execute() 마다 덮어쓴다
            rename_map={},                      # 전처리기가 camera1/2/3 리네임을 이미 한다
            return_to_initial_position=True,    # teardown 에서만 동작
            display_data=False,
            play_sounds=False,                  # 헤드리스라 오디오 장치가 없다. 기본값 True 로
                                                #    두면 재생 시도가 지연/경고를 만든다
            device=self.device,
        )
        logger.info("롤아웃 컨텍스트 구성 (policy=%s, task=%r)", self.policy_path, GROOT_TASK)
        self.ctx = build_rollout_context(cfg, self.shutdown)
        self.strategy = _build_strategy_class()(cfg.strategy)
        self.strategy.setup(self.ctx)           # 추론 엔진 생성 + 워밍업
        self.error = None
        logger.info("GR00T 준비 완료")

    def ready(self) -> bool:
        return self.strategy is not None

    def read_state(self) -> list[float]:
        if self.ctx is None:
            raise RuntimeError("롤아웃 컨텍스트가 아직 없다")
        obs = self.ctx.hardware.robot_wrapper.get_observation()
        state = _extract_state(obs)
        if state is None:
            raise RuntimeError(f"관측에서 관절 상태를 못 뽑았다: {sorted(obs)[:12]}")
        return state

    def latest_frame(self) -> bytes | None:
        """씬 카메라 1장 (JPEG). VLM 이 HTTP 로 받아 간다.

        카메라를 여는 것은 여전히 이 프로세스뿐이다 — ThreadSafeRobot 이 관측 접근을
        직렬화하므로 추론 루프와 동시에 읽어도 안전하다.
        """
        from robot_bridge.frame_server import SCENE_ALIASES, to_jpeg

        if self.ctx is None:
            return None
        obs = self.ctx.hardware.robot_wrapper.get_observation()
        for key in SCENE_ALIASES:
            if key in obs:
                return to_jpeg(obs[key])
        raise RuntimeError(f"씬 카메라를 못 찾았다: {sorted(obs)[:12]}")

    # ── robot_cmd 1건 = open_place_close 1 사이클 ──────────────────────────
    async def execute(self, cmd_id: str, target: str | None, deadline_ms: int,
                      on_progress: ProgressCb) -> RobotOutcome:
        if not self.ready():
            return RobotOutcome(False, "aborted", 0, f"정책 미준비: {self.error}")

        self.shutdown.clear()               # 이전 턴의 중단 신호를 지운다
        self._aborted.discard(cmd_id)
        self.ctx.runtime.cfg.duration = deadline_ms / 1000.0

        violations: list[str] = []
        self.strategy.bind(asyncio.get_running_loop(), on_progress, violations.append)

        started = time.monotonic()
        try:
            await asyncio.to_thread(self.strategy.run, self.ctx)
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.exception("롤아웃 실행 중 예외")
            await self._recover_to_home(f"{type(exc).__name__}: {exc}")
            return RobotOutcome(False, "aborted", elapsed_ms, f"{type(exc).__name__}: {exc}")
        elapsed_ms = int((time.monotonic() - started) * 1000)

        if violations:
            detail = f"preflight: {violations[0]}"
            await self._recover_to_home(detail)
            return RobotOutcome(False, "aborted", elapsed_ms, detail)
        if cmd_id in self._aborted:
            self._aborted.discard(cmd_id)
            await self._recover_to_home("중단 요청")
            return RobotOutcome(False, "aborted", elapsed_ms, "중단 요청")
        if elapsed_ms >= deadline_ms:
            await self._recover_to_home("데드라인 초과")
            return RobotOutcome(False, "timeout", elapsed_ms, "데드라인 초과")
        # 여기서 내는 success 는 "루프가 끝까지 돌았다" 는 뜻이지 물리적 성공이 아니다.
        # 물건이 실제로 가방에 들어갔는지는 ROBOT_VERIFY 의 VLM 이 정한다.
        # 정상 종료는 학습 동작의 자연스러운 끝점이라 홈으로 되감지 않는다 — 그 자체가
        # 검증 안 된 궤적을 매 항목마다 추가로 태우는 셈이라 아래 위험이 매번 반복된다.
        return RobotOutcome(True, "verified", elapsed_ms, "")

    async def _recover_to_home(self, reason: str) -> None:
        """실패한 턴 뒤 팔을 홈 자세로 되돌린다.

        `StartGuard`(연결 시 `robot.connect()` 안에서 1회 생성)는 그 프로세스
        생애의 **첫 틱에만** 시작 자세 불일치를 검사한다(`self._checked` 셋이
        인스턴스 수명 동안 유지). 우리는 기동 시 1회 연결해 계속 상주하므로,
        두 번째 `robot_cmd` 부터는 그 안전장치가 다시 돌지 않는다 — 팔이 사이클
        중간(봉투 위반·타임아웃·중단)에 멈춘 채로 다음 명령을 받으면 아무도
        그 간극을 확인하지 않는다. 여기서 명시적으로 되감아 다음 실행이 항상
        알려진 자세에서 시작하게 한다.

        `openarm_sciedu.py:_go_home()` 이 쓰는 검증된 스윕(`walk_to_home`)을
        그대로 재사용한다 — 레이트 제한·클램프가 이미 들어 있고, 새 모션 계획을
        짜지 않는다.

        ⚠️ **알려진 한계**: 그 스윕의 우회 경로(`clearance_bulge`)는 "정지 자세 →
        작업 자세"라는 **한 가지 전이**만 겨냥해 튜닝됐다(책상 모서리·몸통 기둥
        회피). 사이클 **중간**(가방 위, 물건을 놓는 자세 등)에서 출발할 때도
        같은 우회량을 쓰는데, 그 경로가 안전한지는 검증된 적이 없다. 실물 첫
        시험은 사람이 지켜보며 진행한다.
        """
        logger.warning("실패(%s) 뒤 홈으로 복귀한다 — 다음 명령이 알려진 자세에서 시작하도록", reason)
        try:
            await asyncio.to_thread(self.ctx.hardware.robot_wrapper.inner._go_home)
            logger.info("홈 복귀 완료")
        except Exception:  # noqa: BLE001 — 복귀 실패로 백엔드 전체를 죽이지 않는다
            logger.exception("홈 복귀 실패 — 팔이 마지막 위치에 멈춰 있다. 수동 확인이 필요하다")

    # ── robot_abort · pause · camera all_ok=false ─────────────────────────
    async def abort(self, cmd_id: str, reason: str) -> None:
        self._aborted.add(cmd_id)
        logger.info("중단 요청 cmd_id=%s reason=%s", cmd_id, reason)
        self.shutdown.set()          # run() 이 다음 틱에 빠져나온다

    async def close(self) -> None:
        """프로세스 종료 시 1회. 턴 사이에는 절대 부르지 않는다 —
        하드웨어 disconnect 와 return_to_initial_position 이 일어난다."""
        if self.strategy is not None and self.ctx is not None:
            await asyncio.to_thread(self.strategy.teardown, self.ctx)
            self.strategy = None
