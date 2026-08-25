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

# ── 작업 완료 감지 ───────────────────────────────────────────────────────────
#
# 롤아웃 루프는 `duration` 이 다 될 때까지만 돈다(`strategies/base.py`:
# `while not shutdown_event.is_set(): ... if elapsed >= cfg.duration: break`).
# **완료 감지가 없다.** 그래서 34~46초면 끝나는 동작에 150초를 걸어 두면 남은
# 100초 동안 정책이 학습에 없는 상태에서 계속 액션을 낸다 — 아이는 대기 화면에서
# 그만큼 기다리고, 그 사이 운영자 키는 전부 무시된다(ROBOT_TURN 이 안 끝나서).
#
# 무엇을 신호로 쓸지는 데이터로 골랐다. `nuedive_test_90epi_merged_20260825` 의
# ep0~59(현재 홈과 같은 구간, 60개) 에서 "마지막 큰 움직임 → 에피소드 끝":
#
#     왼쪽 그리퍼   중앙  3.5초  (p10 2.9 / p90 4.8)   ← 쓴다
#     오른쪽 그리퍼 중앙 14.1초  (p10 12.7 / p90 16.1)
#     팔 14축 전체  중앙  1.1초  (p10 0.5 / p90 1.9)   ← 너무 늦다. 끊을 시간이 없다
#
# 관절 각도로 "종료 자세에 가까워졌나"를 보는 방식은 버렸다. 팔이 궤적 중간에
# 종료 자세 근처를 지나가서 오탐이 심하다 — 임계 20도면 59/60 에서 걸리지만
# 실제 종료보다 중앙 44.3초 일찍 걸린다(사실상 시작하자마자). 정지 감지도 안 된다:
# 종료 직전 3초의 관절 속도가 중앙 14.1도/초로 여전히 움직이는 중이다.
#
# 왼쪽 그리퍼는 가방을 잡는 쪽이고, 궤적이 에피소드마다 사실상 같다:
#
#     0~20%  -60(열림)  →  30~80%  ~0(닫힘, 가방을 쥔 채)  →  90~100%  -60(놓음)
#
# 즉 **가방을 닫고 놓는 순간**이 작업의 끝이다. 열림(-60)과 닫힘(0) 사이가 60도라
# 임계 하나로 갈리고 여유가 30도씩 있다.
#
# 한계는 참고한 선행 실험(andyjusa/vla-subtask-phase-probe)이 스스로 적어 둔 것과
# 같다 — 이것은 정책의 **의도**이지 물리적 성공이 아니다. 물건이 실제로 가방에
# 들어갔는지는 여기서 판단하지 않는다. 그 판정은 원래대로 ROBOT_VERIFY 의 VLM
# ("배치면이 비었나 + 가방이 닫혔나")이 한다. 여기서 정하는 것은 **턴을 언제
# 끊을까** 뿐이다.
#
# 실패 방향도 안전한 쪽이다. 정책이 가방을 못 잡으면 그리퍼가 닫히지 않아 감지가
# 아예 발화하지 않고, 턴은 예전처럼 deadline 까지 간다. 반대로 도중에 가방을
# 떨어뜨려 일찍 발화하면 ROBOT_VERIFY 가 `bag_closed=false` 로 잡는다.

#: 왼쪽 그리퍼 열림/닫힘 경계(도). 열림 -60 · 닫힘 0 사이 한가운데.
LEFT_GRIPPER_OPEN_DEG = -30.0
#: 놓은 뒤 이만큼 유지돼야 완료로 본다. 데이터상 놓고 나서 3.5초(p10 2.9)가
#: 남으므로, 1초를 기다려도 자연스러운 끝보다 일찍 끊는다.
COMPLETE_HOLD_S = 1.0
#: 가방을 쥐었다고 인정하는 최소 시간. 시작할 때 그리퍼는 열려 있으므로,
#: "닫힘을 본 적 있다"를 먼저 요구해야 시작 상태를 완료로 오인하지 않는다.
GRIP_CONFIRM_S = 1.0

#: 왼쪽 그리퍼가 `observation.state` 안에서 몇 번째인가. 봉투 정의에서 찾는다 —
#: 인덱스를 손으로 적으면 봉투가 바뀔 때 조용히 어긋난다.
LEFT_GRIPPER_INDEX = STATE_KEYS.index("left_gripper.pos")


class CompletionDetector:
    """왼쪽 그리퍼로 '가방을 닫고 놓았다'를 잡는다. 순수 함수라 리플레이로 검증된다.

    상태는 셋뿐이다.
        열림 대기 → (닫힘을 GRIP_CONFIRM_S 이상 유지) → 쥠
        쥠      → (열림을 COMPLETE_HOLD_S 이상 유지) → 완료
    """

    def __init__(self, *, open_deg: float = LEFT_GRIPPER_OPEN_DEG,
                 hold_s: float = COMPLETE_HOLD_S,
                 confirm_s: float = GRIP_CONFIRM_S) -> None:
        self.open_deg = open_deg
        self.hold_s = hold_s
        self.confirm_s = confirm_s
        self.gripped = False
        self.done = False
        self._closed_since: float | None = None
        self._open_since: float | None = None

    def update(self, left_gripper_deg: float, now_s: float) -> bool:
        """한 틱. 완료로 막 판정된 순간에만 True 를 돌려준다."""
        if self.done:
            return False
        is_open = left_gripper_deg < self.open_deg

        if not self.gripped:
            if is_open:
                self._closed_since = None
                return False
            if self._closed_since is None:
                self._closed_since = now_s
            elif now_s - self._closed_since >= self.confirm_s:
                self.gripped = True
                self._open_since = None
            return False

        if not is_open:
            self._open_since = None
            return False
        if self._open_since is None:
            self._open_since = now_s
            return False
        if now_s - self._open_since >= self.hold_s:
            self.done = True
            return True
        return False



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

        def bind(self, loop, on_progress: ProgressCb, on_violation, on_complete) -> None:
            self._loop = loop
            self._on_progress = on_progress
            self._on_violation = on_violation
            self._on_complete = on_complete
            self._t0 = time.monotonic()
            self._last_tick = -PROGRESS_INTERVAL_S
            self._last_check = -ENVELOPE_INTERVAL_S
            self._state_seen = False
            self._violated = False
            self._detector = CompletionDetector()
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

                # ①-b 작업 완료 — 왼팔이 가방을 닫고 놓았다. 봉투 검사와 같은
                # 5Hz 주기를 쓴다(state 를 여기서만 뽑는다). 감지되면 루프를
                # 세워 남은 deadline 을 태우지 않는다 — 자세한 근거는
                # CompletionDetector 위의 주석에 있다.
                if self._detector.update(state[LEFT_GRIPPER_INDEX], elapsed):
                    logger.info("작업 완료 감지 (왼쪽 그리퍼가 가방을 놓음, %.1fs) — 턴을 끝낸다",
                                elapsed)
                    runtime_ctx.shutdown_event.set()
                    self._loop.call_soon_threadsafe(self._on_complete)
                    return

            # ② progress_tick — 무발화 구간의 유일한 진행 신호.
            if elapsed - self._last_tick >= PROGRESS_INTERVAL_S:
                self._last_tick = elapsed
                # ②-b 그리퍼 진단. **정책이 무엇을 명령했는지**와 팔이 실제로 어디
                # 있는지를 같이 남긴다. 이 둘이 갈라져야만 알 수 있는 것이 있다:
                #   명령이 안 닫힘 → 정책이 잡으려 하지 않는다 (관측·분포 문제)
                #   명령은 닫힘, 실측이 안 따라감 → 구동 문제 (강성·매핑·기구)
                # 로그에 이 값이 없어서 "물체 앞에서 안 잡는다" 를 두 원인 중 어느
                # 쪽으로도 확정하지 못했다. 1초에 한 줄이라 루프에 부담이 없다.
                self._log_grippers(action_dict, state)
                # 제어 스레드에서 이벤트 루프로 넘긴다. 여기서는 await 할 수 없다.
                asyncio.run_coroutine_threadsafe(
                    self._on_progress(int(elapsed * 1000)), self._loop
                )

        @staticmethod
        def _log_grippers(action_dict, state) -> None:
            """명령 vs 실측 그리퍼. 진단이 실패해도 제어 루프를 죽이지 않는다."""
            try:
                cmd = {}
                if isinstance(action_dict, dict):
                    for side in ("left", "right"):
                        v = action_dict.get(f"{side}_gripper.pos")
                        if v is not None:
                            cmd[side] = float(v)
                meas = {}
                if state is not None:
                    meas["left"] = state[STATE_KEYS.index("left_gripper.pos")]
                    meas["right"] = state[STATE_KEYS.index("right_gripper.pos")]
                if cmd or meas:
                    logger.info(
                        "그리퍼 명령/실측 (도) — 좌 %s/%s · 우 %s/%s",
                        f"{cmd['left']:.1f}" if "left" in cmd else "?",
                        f"{meas['left']:.1f}" if "left" in meas else "?",
                        f"{cmd['right']:.1f}" if "right" in cmd else "?",
                        f"{meas['right']:.1f}" if "right" in meas else "?",
                    )
                elif isinstance(action_dict, dict):
                    logger.info("그리퍼 명령 키를 못 찾았다 — 액션 키: %s",
                                sorted(action_dict)[:8])
            except Exception:  # noqa: BLE001 — 진단이 팔을 멈추게 하면 안 된다
                logger.debug("그리퍼 진단 실패", exc_info=True)

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
        completed: list[bool] = []
        self.strategy.bind(asyncio.get_running_loop(), on_progress,
                           violations.append, lambda: completed.append(True))

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
        if completed:
            # 완료 감지가 루프를 세웠다. **아래 shutdown 검사보다 먼저 본다** —
            # 감지도 `shutdown_event` 로 루프를 세우므로 순서가 뒤바뀌면 정상 완료가
            # '프로세스 종료' 로 기록된다.
            # 홈으로 되감지 않는 이유는 아래 성공 경로와 같다.
            return RobotOutcome(True, "verified", elapsed_ms, "")
        if self.shutdown.is_set():
            # 프로세스 종료 신호로 루프가 멈췄다. `_aborted` 에는 없으므로 아래
            # 성공 경로로 새어 나가 '완주'로 기록되던 자리다.
            # 홈으로 되감지 않는다 — 곧 teardown 이 시작 자세 복귀와 하강을 한다.
            return RobotOutcome(False, "aborted", elapsed_ms, "프로세스 종료")
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
        하드웨어 disconnect 와 return_to_initial_position 이 일어난다.

        **`load()` 와 같은 이유로 메인 스레드에서 돈다.** `asyncio.to_thread` 로
        감싸면 정리가 끝나갈 무렵 `ArmSession.__exit__` 이 신호 핸들러를 되돌리다가
        `ValueError: signal only works in main thread` 로 터진다. `shutdown()` 안쪽은
        그 예외를 막아 두었지만 `__exit__` 의 복원 루프(`arm_session.py:239`)는
        막혀 있지 않다.

        실측(dry_run, 2026-08-26): 팔은 내려놓고 토크까지 정상적으로 풀린 **뒤**
        이 예외가 터졌고, 프로세스가 죽지 않아 40초 뒤 SIGKILL 이 필요했다.
        팔은 안전했지만 종료가 매달렸다.

        여기서 이벤트 루프가 몇 초 멈추지만 종료 시점이라 상관없다 — 오히려
        정리가 끝날 때까지 다른 것이 끼어들지 않는 편이 낫다.
        """
        if self.strategy is not None and self.ctx is not None:
            self.strategy.teardown(self.ctx)
            self.strategy = None
