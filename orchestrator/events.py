"""이벤트 계약. CONTRACT.md 의 기계 판독 가능한 원본이다.

CLAUDE_CODE_CONTEXT.md §4.4 를 실측 데이터에 맞게 교정한 판이다. 교정 근거는
CONTRACT.md 에 적혀 있다. 여기서 스키마를 바꾸면 CONTRACT.md 도 같이 고친다.

역할(Role)은 배선 그 자체다. `vlm` 은 `robot` 에게 보낼 수 있는 이벤트 타입이
하나도 없다 — §12 의 "VLM → VLA 직접 호출 금지"를 문서가 아니라 타입으로 강제한다.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class Role(StrEnum):
    TABLET = "tablet"
    OPERATOR = "operator"
    ROBOT = "robot"
    VLM = "vlm"
    CAPTURE = "capture"
    #: 아동의 말을 듣는 스포크. 의도만 보내고 발화는 만들지 않는다 —
    #: 대본은 사전 녹음 고정이다 (§12 "표정·핵심 발화를 VLM 자유 생성에 맡기기" 금지).
    VOICE = "voice"
    #: 발화 재생. 로봇 옆 스피커에서 소리를 내고, 끝나면 advance 를 낸다.
    #: 마이크와 같은 기기라 에코 가드가 성립한다.
    AUDIO = "audio"


class Expression(StrEnum):
    """§5 — 표정은 자유 생성 금지. 이 다섯 개 중 선택만."""

    NEUTRAL = "neutral"
    HAPPY = "happy"
    THINKING = "thinking"
    CELEBRATING = "celebrating"
    WAITING = "waiting"


class PromptLevel(StrEnum):
    """§4.5 프롬프트 위계. 아동이 실패로 끝나는 경로가 없도록 단계적으로 올린다."""

    VERBAL = "verbal"
    HINT = "hint"
    MODEL = "model"


class FailureReason(StrEnum):
    GRASP_FAILED = "grasp_failed"
    TIMEOUT = "timeout"
    ESTOP = "estop"
    HARDWARE_ERROR = "hardware_error"
    PREFLIGHT = "preflight"
    UNKNOWN = "unknown"


class DoneReason(StrEnum):
    VERIFIED = "verified"
    TIMEOUT = "timeout"
    OPERATOR = "operator"
    ABORTED = "aborted"


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- 인바운드
# 오케스트레이터가 받는 이벤트. 각 타입은 하나의 역할에만 허용된다 (ALLOWED_SENDERS).


class SessionStart(_Event):
    type: Literal["session_start"] = "session_start"
    scenario_id: str
    child_id: str | None = None


class ChildPlaced(_Event):
    """판정 트리거 — 아동이 물건을 놓고 물러난 뒤.

    `capture` 가 자동 발행하거나(변화 → 정지 → VLM 이 물체를 봄) 운영자가 버튼으로 낸다.
    운영자 경로는 폴백으로 **끝까지 유지한다** (§12).
    """

    type: Literal["child_placed"] = "child_placed"


class Judge(_Event):
    """VLM → Orchestrator. object 는 판정과 반드시 함께 온다 (§4.4)."""

    type: Literal["judge"] = "judge"
    object: str
    should_pack: bool
    confidence: float = Field(ge=0.0, le=1.0)


class JudgeOverride(_Event):
    """운영자 수동 판정. 데모까지 유지되는 폴백 경로 (§12)."""

    type: Literal["judge_override"] = "judge_override"
    object: str
    should_pack: bool


class VlmHold(_Event):
    """§5 — confidence 가 낮거나 `other`/`none` 이면 판정을 내지 않고 사람에게 넘긴다.

    VLM 이 추측해서 답을 내는 것보다, 판정을 비우고 운영자를 부르는 편이 안전하다.
    로봇이 엉뚱한 물건을 집는 경로를 여기서 끊는다.
    """

    type: Literal["vlm_hold"] = "vlm_hold"
    object: str
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""


class VerifyResult(_Event):
    """ROBOT_TURN 이후 사후 확인. 물체 삽입 여부와 가방 상태를 함께 본다."""

    type: Literal["verify_result"] = "verify_result"
    cmd_id: str
    object_in_bag: bool
    bag_closed: bool


class ForceState(_Event):
    type: Literal["force_state"] = "force_state"
    phase: str


class RobotAbort(_Event):
    type: Literal["robot_abort"] = "robot_abort"
    cmd_id: str | None = None
    reason: str = "operator"


class RobotDone(_Event):
    """`success` 는 필수다. '넣기 성공' 과 '타임아웃으로 떨어뜨림' 을 로그에서
    구분하지 못하면 §7 의 평가 근거 데이터가 성립하지 않는다."""

    type: Literal["robot_done"] = "robot_done"
    cmd_id: str
    success: bool
    reason: DoneReason
    duration_ms: int


class RobotError(_Event):
    type: Literal["robot_error"] = "robot_error"
    cmd_id: str | None = None
    reason: FailureReason = FailureReason.UNKNOWN
    detail: str = ""


class ProgressTick(_Event):
    """로봇 턴이 30초대라 아동에게 비언어적 지속 신호가 필요하다 (CONTRACT §B5)."""

    type: Literal["progress_tick"] = "progress_tick"
    cmd_id: str
    elapsed_ms: int


class ContactAnomaly(_Event):
    type: Literal["contact_anomaly"] = "contact_anomaly"
    joint: str
    deviation: float


class CameraHealth(_Event):
    """카메라 3대 전부가 정책 필수 입력이다. 한 대만 끊겨도 추론을 막아야 한다."""

    type: Literal["camera_health"] = "camera_health"
    cameras: dict[str, bool]
    all_ok: bool


class Pause(_Event):
    type: Literal["pause"] = "pause"


class Resume(_Event):
    type: Literal["resume"] = "resume"


class BagReset(_Event):
    """운영자가 가방을 수동 복구했음을 알린다. 다음 턴의 시작 상태를 되돌린다."""

    type: Literal["bag_reset"] = "bag_reset"


class Intent(StrEnum):
    """아동 발화의 의도. **작은 닫힌 집합**이다.

    전사가 틀려도 의도가 맞으면 시스템은 정상이다 — stt-eval 이 도달한 결론이
    (`experiment_id` 정확도가 주지표, CER 은 보조지표) 여기서도 그대로 성립한다.
    """

    #: "뭐라고?" "다시 말해줘" — Ran 하위과제 3 의 **재호명 요청** 지표.
    REPEAT_REQUEST = "repeat_request"
    #: "모르겠어" "어디 있어?" — 난이도 신호다. 촉진을 한 단계 올린다.
    DONT_KNOW = "dont_know"
    #: "쉬고 싶어" "그만" — 감각 과부하.
    BREAK = "break"
    #: "다 했어" "여기 있어" — 판정 트리거가 **아니다.** 판정은 감시가 한다.
    DONE = "done"
    OTHER = "other"


class ChildUtterance(_Event):
    """Voice → Orchestrator. 이 이벤트가 하나도 안 와도 세션은 완결된다.

    STT 는 가산적으로 붙는다. 마이크가 없거나 인식이 안 되면 그냥 조용할 뿐이다.
    """

    type: Literal["child_utterance"] = "child_utterance"
    text: str = ""
    intent: Intent = Intent.OTHER
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)


class ZoneDisturbed(_Event):
    """`guard` 모드에서 배치 구역에 변화가 감지됐다 — 로봇이 움직이는 중인데.

    판정이 아니다. VLM 도 부르지 않는다. 운영자에게 알리기 위한 신호일 뿐이다.
    """

    type: Literal["zone_disturbed"] = "zone_disturbed"
    detail: str = ""


class Timeout(_Event):
    """상태 머신이 소유하는 내부 타이머. 브릿지가 아니라 여기서 만료를 판정한다."""

    type: Literal["timeout"] = "timeout"
    timer: str
    cmd_id: str | None = None


class Advance(_Event):
    """발화·애니메이션이 끝나 다음 상태로 넘어가도 된다는 내부 신호."""

    type: Literal["advance"] = "advance"
    from_phase: str | None = None


InboundEvent = Annotated[
    SessionStart
    | ChildPlaced
    | Judge
    | JudgeOverride
    | VlmHold
    | VerifyResult
    | ForceState
    | RobotAbort
    | RobotDone
    | RobotError
    | ProgressTick
    | ContactAnomaly
    | CameraHealth
    | Pause
    | Resume
    | BagReset
    | ChildUtterance
    | ZoneDisturbed
    | Timeout
    | Advance,
    Field(discriminator="type"),
]


class InboundEnvelope(BaseModel):
    """WS 로 들어온 raw dict 를 검증하는 진입점."""

    model_config = ConfigDict(extra="forbid")
    event: InboundEvent


def parse_inbound(payload: dict) -> InboundEvent:
    return InboundEnvelope(event=payload).event


# ------------------------------------------------------- 역할별 허용 이벤트
# VLM 은 robot_* 를 보낼 수 없다. 허브-스포크를 타입으로 강제하는 지점.

ALLOWED_SENDERS: dict[str, frozenset[Role]] = {
    "session_start": frozenset({Role.OPERATOR}),
    "child_placed": frozenset({Role.OPERATOR, Role.CAPTURE}),
    "judge": frozenset({Role.VLM}),
    "judge_override": frozenset({Role.OPERATOR}),
    "vlm_hold": frozenset({Role.VLM}),
    "verify_result": frozenset({Role.VLM, Role.OPERATOR}),
    "force_state": frozenset({Role.OPERATOR}),
    "robot_abort": frozenset({Role.OPERATOR}),
    "robot_done": frozenset({Role.ROBOT}),
    "robot_error": frozenset({Role.ROBOT}),
    "progress_tick": frozenset({Role.ROBOT}),
    "contact_anomaly": frozenset({Role.ROBOT}),
    "camera_health": frozenset({Role.CAPTURE, Role.ROBOT}),
    "zone_disturbed": frozenset({Role.CAPTURE}),
    "child_utterance": frozenset({Role.VOICE, Role.OPERATOR}),
    "pause": frozenset({Role.OPERATOR}),
    "resume": frozenset({Role.OPERATOR}),
    "bag_reset": frozenset({Role.OPERATOR}),
    "timeout": frozenset(),  # 내부 전용
    "advance": frozenset({Role.AUDIO, Role.TABLET, Role.OPERATOR}),
}


def sender_allowed(event_type: str, role: Role) -> bool:
    return role in ALLOWED_SENDERS.get(event_type, frozenset())


# ---------------------------------------------------------------- 아웃바운드


class Progress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    packed: list[str] = Field(default_factory=list)
    remaining: list[str] = Field(default_factory=list)


class StateEvent(_Event):
    """Orchestrator → Tablet/Operator. 아동 화면이 구독하는 유일한 이벤트."""

    type: Literal["state"] = "state"
    phase: str
    expression: Expression
    utterance_id: str | None = None
    utterance_text: str | None = None  # VLM 생성 발화만 여기에 싣는다 (§4.4)
    target: str | None = None
    prompt_level: PromptLevel = PromptLevel.VERBAL
    retry: int = 0
    progress: Progress = Field(default_factory=Progress)


class RobotCmd(_Event):
    """Orchestrator → Robot. 정답일 때만 발행된다.

    `motion` 은 `open_place_close` 하나뿐이다 — 학습된 1 에피소드가
    가방 열기 → 물건 1개 넣기 → 가방 닫기 전체이기 때문이다.
    `target` 은 로깅·사후검증용이며 **정책 입력이 아니다**.
    """

    type: Literal["robot_cmd"] = "robot_cmd"
    cmd_id: str
    motion: Literal["open_place_close"] = "open_place_close"
    target: str | None = None
    deadline_ms: int


OutboundEvent = StateEvent | RobotCmd | RobotAbort
