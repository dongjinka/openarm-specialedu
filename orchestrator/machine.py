"""세션 상태 머신. **I/O 가 없다.**

    (SessionState, Event) -> (SessionState, [Effect])

시계조차 인자로 받는다(`now_ms`). 그래서 서버 없이 전이를 전수 테스트할 수 있고,
JSONL 로그를 그대로 리플레이할 수 있으며, 운영자의 WoZ 오버라이드가 특수 경로가
아니라 그냥 또 하나의 이벤트가 된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from orchestrator import events as ev
from orchestrator.effects import (
    Broadcast,
    CancelTimer,
    Effect,
    Log,
    RequestJudge,
    RequestVerify,
    SendRobot,
    SetListen,
    SetWatch,
    StartTimer,
)
from orchestrator.events import PromptLevel, Progress, StateEvent
from orchestrator.scenario import Scenario
from orchestrator.states import FORCEABLE, PRESENTATION, Phase

TIMER_JUDGE = "judge"
TIMER_ROBOT = "robot"
TIMER_VERIFY = "verify"
TIMER_STALL = "stall"

#: 촉진 위계가 상한에 닿은 뒤 이만큼 더 정체하면 운영자를 부른다.
STALL_ALERT_AFTER = 3


@dataclass(frozen=True)
class SessionState:
    phase: Phase = Phase.IDLE
    packed: tuple[str, ...] = ()
    prompt_level: PromptLevel = PromptLevel.VERBAL
    retry: int = 0
    cmd_seq: int = 0
    #: 현재 유효한 명령. 만료된 cmd_id 의 늦은 robot_done 을 걸러내는 세대 가드.
    pending_cmd_id: str | None = None
    pending_object: str | None = None
    paused_from: Phase | None = None
    cameras_ok: bool = True
    bag_ok: bool = True
    request_at_ms: int = 0
    #: 아동이 물건을 올린 것이 확인된 시점. **판정 시점과 다르다** —
    #: 그 사이에 정지 대기(≈1초)와 VLM 호출(중앙 3.6초)이 들어간다.
    #: Ran 하위과제 4 의 '탐색시간' 은 이 값으로 재야 VLM 지연이 안 섞인다.
    placed_at_ms: int = 0
    turn_started_ms: int = 0
    #: 배치 구역 감시 모드. 상태에 두는 이유는 전이마다 SetWatch 를 남발하지 않기 위해서다.
    watch_mode: str = "off"
    #: 아무 변화 없이 흘러간 정체 횟수. 오답 재시도(retry)와 **함께** 촉진 위계를 밀어올린다.
    stall_count: int = 0
    #: 마이크 창. watch_mode 와 같은 이유로 상태에 둔다 (전이마다 명령을 남발하지 않기 위해).
    listen_mode: str = "off"
    #: 아동이 "다시 말해줘" 라고 한 횟수 — Ran 하위과제 3 의 작업기억 지표.
    repeat_requests: int = 0

    def remaining(self, scenario: Scenario) -> tuple[str, ...]:
        return tuple(o for o in scenario.checklist if o not in self.packed)

    def target(self, scenario: Scenario) -> str | None:
        rest = self.remaining(scenario)
        return rest[0] if rest else None


@dataclass(frozen=True)
class Transition:
    state: SessionState
    effects: list[Effect] = field(default_factory=list)


def _prompt_level_for(retry: int, scenario: Scenario) -> PromptLevel:
    """§4.5 프롬프트 위계. 오답 2회 → 힌트, 3회 → 로봇이 가리키기.

    상한을 넘어도 MODEL 에 머무를 뿐 실패로 끝나는 경로는 만들지 않는다.
    """
    ladder = [PromptLevel(p) for p in scenario.prompt_hierarchy]
    if not ladder:
        return PromptLevel.VERBAL
    return ladder[min(retry, len(ladder) - 1)]


#: 감시는 두 구간에서만 돈다. 그 밖에서는 끈다.
#:   WAIT_CHILD — 판정 모드. 변화 → 정지 → VLM 1회.
#:   ROBOT_TURN — 경고 모드. VLM 을 부르지 않고, 물건이 올라오면 운영자만 부른다.
_WATCH_MODE: dict[Phase, str] = {
    Phase.WAIT_CHILD: "judge",
    Phase.ROBOT_TURN: "guard",
}


def _watch_for(phase: Phase) -> str:
    return _WATCH_MODE.get(phase, "off")


#: 마이크는 아동이 말할 수 있는 구간에서만 연다.
#:
#: 로봇이 말하는 동안 열면 자기 목소리를 되받고, 팔이 도는 동안 열면 모터 소음
#: (1~2kHz +13dB 실측)을 받는다. 창을 우리가 소유하면 둘 다 애초에 일어나지 않는다.
#: REQUEST 는 아직 발화 중이라 뺐다 — WAIT_CHILD 로 넘어가야 연다.
_LISTEN_MODE: dict[Phase, str] = {
    Phase.WAIT_CHILD: "open",
}


def _listen_for(phase: Phase) -> str:
    return _LISTEN_MODE.get(phase, "off")


#: 이 상태들에서 태블릿이 강조할 항목은 '다음에 챙길 것' 이 아니라
#: '지금 로봇이 다루고 있는 것' 이다. CORRECT 에서 ✓ 가 엉뚱한 항목에 붙으면 안 된다.
_PENDING_TARGET_PHASES = frozenset(
    {Phase.CORRECT, Phase.ROBOT_TURN, Phase.ROBOT_VERIFY, Phase.ROBOT_FAIL}
)


def _default_target(state: SessionState, scenario: Scenario) -> str | None:
    if state.phase in _PENDING_TARGET_PHASES and state.pending_object:
        return state.pending_object
    return state.target(scenario)


def _state_event(state: SessionState, scenario: Scenario, **over) -> dict:
    pres = PRESENTATION[state.phase]
    payload = StateEvent(
        phase=state.phase.value,
        expression=over.pop("expression", pres.expression),
        utterance_id=over.pop("utterance_id", None if pres.silent else pres.utterance_id),
        target=over.pop("target", _default_target(state, scenario)),
        prompt_level=state.prompt_level,
        retry=state.retry,
        progress=Progress(packed=list(state.packed), remaining=list(state.remaining(scenario))),
        **over,
    )
    return payload.model_dump(mode="json")


def _enter(state: SessionState, scenario: Scenario, phase: Phase, now_ms: int) -> Transition:
    """상태 진입. 진입 시점에만 일어나는 일(발화·타이머·즉시 해소)을 모은다."""
    was_waiting = state.phase is Phase.WAIT_CHILD
    state = replace(state, phase=phase)
    effects: list[Effect] = []

    # WAIT_CHILD 를 떠나면 정체 타이머는 의미가 없다.
    if was_waiting and phase is not Phase.WAIT_CHILD:
        effects.append(CancelTimer(TIMER_STALL))

    # 감시 모드는 상태와 함께 움직인다. 바뀔 때만 명령을 낸다.
    mode = _watch_for(phase)
    if mode != state.watch_mode:
        state = replace(state, watch_mode=mode)
        effects.append(SetWatch(mode))

    listen = _listen_for(phase)
    if listen != state.listen_mode:
        state = replace(state, listen_mode=listen)
        effects.append(SetListen(listen))

    if phase is Phase.REQUEST:
        target = state.target(scenario)
        if target is None:
            return _enter(state, scenario, Phase.COMPLETE, now_ms)
        state = replace(
            state, prompt_level=_prompt_level_for(state.retry + state.stall_count, scenario)
        )
        effects.append(
            Broadcast(
                _state_event(
                    state,
                    scenario,
                    utterance_id=scenario.utterance(target, state.prompt_level.value),
                )
            )
        )
        effects.append(
            Log("state_change", {"phase": phase.value, "target": target, "trial": state.retry + 1,
                                 "prompt_level": state.prompt_level.value})
        )
        return Transition(state, effects)

    if phase is Phase.WAIT_CHILD:
        # 아동이 행동할 수 있게 된 시점 — 반응 시간의 기준점 (§7).
        state = replace(state, request_at_ms=now_ms, placed_at_ms=0)
        # 아무것도 하지 않고 멈춰 있으면 변화 구동 감시는 영영 깨어나지 않는다.
        effects.append(StartTimer(TIMER_STALL, scenario.stall_timeout_ms))

    if phase is Phase.JUDGE:
        # thinking 을 '즉시' 표시해 판정 지연의 공백을 메운다 (§4.5).
        effects.append(Broadcast(_state_event(state, scenario)))
        effects.append(
            RequestJudge(
                checklist=list(scenario.checklist),
                packed=list(state.packed),
                target=state.target(scenario),
            )
        )
        effects.append(StartTimer(TIMER_JUDGE, scenario.judge_timeout_ms))
        return Transition(state, effects)

    if phase is Phase.ROBOT_TURN:
        # 카메라 3대가 모두 살아 있지 않으면 추론 자체를 시작하지 않는다.
        if not state.cameras_ok:
            blocked = _enter(replace(state, pending_cmd_id=None), scenario, Phase.ROBOT_FAIL, now_ms)
            return Transition(
                blocked.state,
                [Log("robot_blocked", {"reason": "camera_unhealthy"})] + blocked.effects,
            )
        seq = state.cmd_seq + 1
        cmd_id = f"c{seq}"
        target = state.pending_object
        state = replace(state, cmd_seq=seq, pending_cmd_id=cmd_id, turn_started_ms=now_ms)
        cmd = ev.RobotCmd(cmd_id=cmd_id, target=target, deadline_ms=scenario.robot_deadline_ms)
        effects.append(Broadcast(_state_event(state, scenario, target=target)))
        effects.append(SendRobot(cmd.model_dump(mode="json")))
        effects.append(StartTimer(TIMER_ROBOT, scenario.robot_deadline_ms, cmd_id=cmd_id))
        effects.append(
            Log("robot_cmd", {"cmd_id": cmd_id, "motion": "open_place_close", "target": target})
        )
        return Transition(state, effects)

    if phase is Phase.ROBOT_VERIFY:
        effects.append(Broadcast(_state_event(state, scenario)))
        effects.append(RequestVerify(cmd_id=state.pending_cmd_id or "", target=state.pending_object))
        effects.append(StartTimer(TIMER_VERIFY, scenario.verify_timeout_ms))
        return Transition(state, effects)

    if phase is Phase.NEXT:
        # 전이 상태 — 머무르지 않는다.
        effects.append(Log("state_change", {"phase": phase.value}))
        nxt = Phase.COMPLETE if not state.remaining(scenario) else Phase.REQUEST
        follow = _enter(
            replace(state, retry=0, stall_count=0, prompt_level=PromptLevel.VERBAL),
            scenario, nxt, now_ms,
        )
        return Transition(follow.state, effects + follow.effects)

    effects.append(Broadcast(_state_event(state, scenario)))
    effects.append(Log("state_change", {"phase": phase.value}))
    return Transition(state, effects)


def handle(
    state: SessionState, event: ev.InboundEvent, scenario: Scenario, *, now_ms: int = 0
) -> Transition:
    """단일 진입점. 이 함수 밖에서 상태가 바뀌는 곳은 없다."""
    t = event.type

    # ---- 어느 상태에서나 받는 것들 -------------------------------------
    if t == "camera_health":
        changed = state.cameras_ok != event.all_ok
        state = replace(state, cameras_ok=event.all_ok)
        effects: list[Effect] = []
        if changed:
            effects.append(Log("camera_health", {"all_ok": event.all_ok, "cameras": event.cameras}))
        # 동작 중 카메라가 죽으면 정책이 잘못된 관측으로 계속 움직인다 → 즉시 중단.
        if not event.all_ok and state.phase is Phase.ROBOT_TURN:
            effects.append(SendRobot({"type": "robot_abort", "cmd_id": state.pending_cmd_id,
                                      "reason": "camera_unhealthy"}))
            effects.append(CancelTimer(TIMER_ROBOT))
            tr = _enter(state, scenario, Phase.ROBOT_FAIL, now_ms)
            return Transition(tr.state, effects + tr.effects)
        return Transition(state, effects)

    if t == "child_utterance":
        return _handle_utterance(state, event, scenario, now_ms)

    if t == "zone_disturbed":
        # 로봇이 움직이는 중에 배치 구역이 흔들렸다. 판정하지 않는다 —
        # 이송 중 새 물건이 책상에 올라오면 정책은 학습에서 본 적 없는 관측을 받는다
        # (60에피소드 이송 구간에 물건이 놓인 프레임은 하나도 없다).
        # 자동 중단도 하지 않는다: 이송 중 abort 는 들고 있던 물건을 떨어뜨린다.
        # 운영자가 치우거나 robot_abort/pause 를 쓴다.
        if state.phase is not Phase.ROBOT_TURN:
            return Transition(state, [Log("zone_disturbed_ignored", {"phase": state.phase.value})])
        return Transition(state, [
            Log("zone_disturbed", {"cmd_id": state.pending_cmd_id, "detail": event.detail}),
            Broadcast({"type": "operator_attention", "reason": "zone_disturbed",
                       "cmd_id": state.pending_cmd_id, "detail": event.detail},
                      roles=(ev.Role.OPERATOR,)),
        ])

    if t == "contact_anomaly":
        return Transition(state, [Log("contact_anomaly", {"joint": event.joint,
                                                          "deviation": event.deviation})])

    if t == "progress_tick":
        if event.cmd_id != state.pending_cmd_id:
            return Transition(state, [Log("stale_progress", {"cmd_id": event.cmd_id})])
        # 무발화 구간의 비언어적 지속 신호. 스톱워치에 **단계 이름**을 얹는다 —
        # 46초(실행은 더 길 수 있음) 동안 "얼마나 남았나"를 숫자 없이 알려주기 위해서다.
        # 시간 기준 근사이므로 표시용이다. 성공 판정은 ROBOT_VERIFY 의 VLM 이 한다.
        ratio = (event.elapsed_ms / scenario.robot_deadline_ms
                 if scenario.robot_deadline_ms > 0 else 0.0)
        payload = {"type": "progress_tick", "cmd_id": event.cmd_id,
                   "elapsed_ms": event.elapsed_ms, "ratio": round(min(ratio, 1.0), 3)}
        phase_id = scenario.turn_phase(ratio)
        if phase_id:
            payload["turn_phase"] = phase_id
        return Transition(state, [Broadcast(payload)])

    if t == "pause":
        if state.phase is Phase.PAUSED:
            return Transition(state, [])
        effects = [Log("pause", {"from": state.phase.value})]
        if state.phase is Phase.ROBOT_TURN:
            # 안전 우선 — 일시정지는 팔을 멈춘다. 재개는 턴을 처음부터 다시 시작한다.
            effects.append(SendRobot({"type": "robot_abort", "cmd_id": state.pending_cmd_id,
                                      "reason": "pause"}))
            effects.append(CancelTimer(TIMER_ROBOT))
        paused = replace(state, paused_from=state.phase)
        tr = _enter(paused, scenario, Phase.PAUSED, now_ms)
        return Transition(tr.state, effects + tr.effects)

    if t == "resume":
        if state.phase is not Phase.PAUSED:
            return Transition(state, [])
        back = state.paused_from or Phase.REQUEST
        # 동작 중에 멈췄다면 중간부터 잇지 않고 CORRECT 로 돌아가 턴을 다시 낸다.
        if back is Phase.ROBOT_TURN:
            back = Phase.CORRECT
        tr = _enter(replace(state, paused_from=None), scenario, back, now_ms)
        return Transition(tr.state, [Log("resume", {"to": back.value})] + tr.effects)

    if t == "bag_reset":
        return Transition(replace(state, bag_ok=True), [Log("bag_reset", {})])

    if t == "force_state":
        try:
            phase = Phase(event.phase)
        except ValueError:
            return Transition(state, [Log("force_state_rejected", {"phase": event.phase})])
        if phase not in FORCEABLE:
            return Transition(state, [Log("force_state_rejected", {"phase": event.phase})])
        tr = _enter(state, scenario, phase, now_ms)
        return Transition(tr.state, [Log("force_state", {"phase": phase.value})] + tr.effects)

    if t == "robot_abort":
        if state.phase is not Phase.ROBOT_TURN:
            return Transition(state, [Log("abort_ignored", {"phase": state.phase.value})])
        effects = [
            SendRobot({"type": "robot_abort", "cmd_id": state.pending_cmd_id, "reason": event.reason}),
            CancelTimer(TIMER_ROBOT),
            Log("robot_abort", {"cmd_id": state.pending_cmd_id, "reason": event.reason}),
        ]
        tr = _enter(state, scenario, Phase.ROBOT_VERIFY, now_ms)
        return Transition(tr.state, effects + tr.effects)

    # ---- 상태별 -------------------------------------------------------
    if state.phase is Phase.IDLE and t == "session_start":
        # 카메라·가방은 세션이 아니라 설비의 상태다. 새 세션이 지워버리면 안 된다.
        fresh = SessionState(cameras_ok=state.cameras_ok, bag_ok=state.bag_ok)
        tr = _enter(fresh, scenario, Phase.INTRO, now_ms)
        return Transition(tr.state, [Log("session_start", {"scenario_id": scenario.scenario_id})] + tr.effects)

    if state.phase is Phase.INTRO and t == "advance":
        return _enter(state, scenario, Phase.SHOW_LIST, now_ms)

    if state.phase is Phase.SHOW_LIST and t == "advance":
        return _enter(state, scenario, Phase.REQUEST, now_ms)

    if state.phase is Phase.REQUEST and t == "advance":
        return _enter(state, scenario, Phase.WAIT_CHILD, now_ms)

    if state.phase is Phase.WAIT_CHILD and t == "child_placed":
        return _enter(replace(state, placed_at_ms=now_ms), scenario, Phase.JUDGE, now_ms)

    if state.phase is Phase.WAIT_CHILD and t == "advance":
        # 촉진 발화가 끝났다. 상태는 그대로 두고 **마이크만 다시 연다** —
        # 재생 중에는 닫아 두었다(에코). 이 신호가 없으면 한 번 촉진한 뒤로
        # 아동의 말이 영영 안 들린다.
        if state.listen_mode == "open":
            return Transition(state, [])
        return Transition(replace(state, listen_mode="open"), [SetListen("open")])

    if state.phase is Phase.WAIT_CHILD and t == "timeout" and event.timer == TIMER_STALL:
        return _handle_stall(state, scenario, now_ms)

    if state.phase is Phase.JUDGE and t in ("judge", "judge_override"):
        return _resolve_judge(state, event, scenario, now_ms)

    if state.phase is Phase.JUDGE and t == "vlm_hold":
        # 판정을 내지 않고 JUDGE 에 머문다. 운영자가 judge_override 로 확정한다.
        return Transition(state, [
            CancelTimer(TIMER_JUDGE),
            Log("vlm_hold", {"object": event.object, "confidence": event.confidence,
                             "reason": event.reason}),
            Broadcast({"type": "operator_attention", "reason": "vlm_hold",
                       "object": event.object, "confidence": event.confidence,
                       "detail": event.reason}, roles=(ev.Role.OPERATOR,)),
        ])

    if state.phase is Phase.JUDGE and t == "timeout" and event.timer == TIMER_JUDGE:
        # 판정 보류 — 운영자에게 넘긴다. 자동으로 진행하지 않는다 (§5).
        return Transition(state, [Log("judge_timeout", {}),
                                  Broadcast({"type": "operator_attention", "reason": "judge_timeout"},
                                            roles=(ev.Role.OPERATOR,))])

    if state.phase in (Phase.CORRECT, Phase.INCORRECT, Phase.DUPLICATE) and t == "advance":
        if state.phase is Phase.CORRECT:
            return _enter(state, scenario, Phase.ROBOT_TURN, now_ms)
        return _enter(state, scenario, Phase.REQUEST, now_ms)

    if state.phase is Phase.ROBOT_TURN:
        if t == "robot_done":
            if event.cmd_id != state.pending_cmd_id:
                return Transition(state, [Log("stale_robot_done", {"cmd_id": event.cmd_id})])
            effects = [
                CancelTimer(TIMER_ROBOT),
                Log("robot_done", {"cmd_id": event.cmd_id, "success": event.success,
                                   "reason": event.reason.value, "duration_ms": event.duration_ms}),
            ]
            tr = _enter(state, scenario, Phase.ROBOT_VERIFY, now_ms)
            return Transition(tr.state, effects + tr.effects)
        if t == "robot_error":
            effects = [CancelTimer(TIMER_ROBOT),
                       Log("robot_error", {"cmd_id": event.cmd_id, "reason": event.reason.value,
                                           "detail": event.detail})]
            tr = _enter(state, scenario, Phase.ROBOT_FAIL, now_ms)
            return Transition(tr.state, effects + tr.effects)
        if t == "timeout" and event.timer == TIMER_ROBOT:
            # 타임아웃이어도 물건이 들어갔을 수 있다 — 실패로 단정하지 않고 확인한다.
            effects = [
                SendRobot({"type": "robot_abort", "cmd_id": state.pending_cmd_id, "reason": "timeout"}),
                Log("robot_timeout", {"cmd_id": state.pending_cmd_id,
                                      "elapsed_ms": now_ms - state.turn_started_ms}),
            ]
            tr = _enter(state, scenario, Phase.ROBOT_VERIFY, now_ms)
            return Transition(tr.state, effects + tr.effects)

    if state.phase is Phase.ROBOT_VERIFY:
        if t == "verify_result":
            return _resolve_verify(state, event, scenario, now_ms)
        if t == "timeout" and event.timer == TIMER_VERIFY:
            tr = _enter(state, scenario, Phase.ROBOT_FAIL, now_ms)
            return Transition(tr.state, [Log("verify_timeout", {"cmd_id": state.pending_cmd_id})] + tr.effects)

    if state.phase is Phase.ROBOT_FAIL and t == "advance":
        # 이 물건은 포기하고 넘어간다. 아동에게는 로봇의 실수로 안내된 뒤다.
        return _enter(state, scenario, Phase.NEXT, now_ms)

    if state.phase is Phase.COMPLETE and t == "advance":
        return _enter(state, scenario, Phase.END, now_ms)

    return Transition(state, [Log("event_ignored", {"phase": state.phase.value, "event_type": t})])


def _resolve_judge(
    state: SessionState, event, scenario: Scenario, now_ms: int
) -> Transition:
    obj = event.object
    overridden = event.type == "judge_override"
    confidence = getattr(event, "confidence", None)
    target = state.target(scenario)
    latency_ms = max(0, now_ms - state.request_at_ms)
    on_target = obj == target

    effects: list[Effect] = [
        CancelTimer(TIMER_JUDGE),
        Log("vlm_judge", {"object": obj, "should_pack": event.should_pack,
                          "confidence": confidence, "overridden": overridden}),
    ]

    if obj in state.packed:
        outcome, nxt = "duplicate", Phase.DUPLICATE
    elif event.should_pack and (on_target or not scenario.strict_order):
        outcome, nxt = "correct", Phase.CORRECT
    else:
        outcome, nxt = "incorrect", Phase.INCORRECT

    # 탐색시간: 요청 → 물건을 올린 순간. 판정 지연이 섞이지 않는다 (Ran 하위과제 4).
    # 운영자 버튼 폴백에서는 placed_at_ms 가 눌린 시점이라 사람 반응시간이 섞인다.
    search_ms = (max(0, state.placed_at_ms - state.request_at_ms)
                 if state.placed_at_ms else None)
    effects.append(
        Log("child_response", {"object": obj, "correct": outcome == "correct",
                               "outcome": outcome, "on_target": on_target,
                               "requested": target,
                               "search_ms": search_ms,
                               "judge_ms": (latency_ms - search_ms) if search_ms is not None else None,
                               "latency_ms": latency_ms,
                               "prompt_level": state.prompt_level.value,
                               "retry": state.retry,
                               "independent": state.prompt_level == PromptLevel.VERBAL})
    )

    if outcome == "correct":
        # 태블릿에 ✓ 를 즉시 띄우기 위해 여기서 담는다. 사후 확인이 실패하면 되돌린다.
        state = replace(state, packed=state.packed + (obj,), pending_object=obj,
                        retry=0, stall_count=0)
    elif outcome == "incorrect":
        state = replace(state, retry=state.retry + 1)
        if state.retry >= scenario.max_retries_per_item:
            effects.append(Log("max_retries_reached", {"target": target, "retry": state.retry}))

    tr = _enter(state, scenario, nxt, now_ms)
    return Transition(tr.state, effects + tr.effects)


def _resolve_verify(
    state: SessionState, event, scenario: Scenario, now_ms: int
) -> Transition:
    if event.cmd_id and state.pending_cmd_id and event.cmd_id != state.pending_cmd_id:
        return Transition(state, [Log("stale_verify", {"cmd_id": event.cmd_id})])

    ok = event.object_in_bag and event.bag_closed
    effects: list[Effect] = [
        CancelTimer(TIMER_VERIFY),
        Log("robot_verify", {"cmd_id": state.pending_cmd_id, "object": state.pending_object,
                             "object_in_bag": event.object_in_bag,
                             "bag_closed": event.bag_closed, "success": ok}),
    ]
    state = replace(state, bag_ok=event.bag_closed)

    if ok:
        state = replace(state, pending_cmd_id=None, pending_object=None)
        tr = _enter(state, scenario, Phase.NEXT, now_ms)
        return Transition(tr.state, effects + tr.effects)

    # 실패 — 담았다고 표시했던 것을 되돌려 다시 요청할 수 있게 한다.
    if state.pending_object and state.pending_object in state.packed:
        state = replace(state, packed=tuple(o for o in state.packed if o != state.pending_object))
    tr = _enter(state, scenario, Phase.ROBOT_FAIL, now_ms)
    return Transition(tr.state, effects + tr.effects)


def _handle_stall(state: SessionState, scenario: Scenario, now_ms: int) -> Transition:
    """WAIT_CHILD 에서 아무 변화 없이 시간이 흘렀다.

    변화 구동 감시는 변화가 없으면 깨어나지 않는다. 아동이 오답 물건을 치우지도, 새
    물건을 올리지도 않고 멈춰 있으면 세션이 그대로 정지한다. 그래서 타이머로 촉진한다.

    촉진은 오답과 **같은 위계**를 쓴다 (`verbal → hint → model`). 상한에 닿으면 `model`
    에 머무르며 로봇이 정답을 가리켜 준다 — 실패로 끝나는 경로는 만들지 않는다.
    `prompt_given` 로그가 Ran 하위과제 2 의 '촉진 횟수' 지표가 된다.

    REQUEST 로 되돌아가지 않는다. 되돌아가면 태블릿이 advance 를 한 번 더 보내야 하고
    반응 시간 기준점(`request_at_ms`)이 리셋돼 §7 지표가 망가진다.
    """
    stalls = state.stall_count + 1
    level = _prompt_level_for(state.retry + stalls, scenario)
    state = replace(state, stall_count=stalls)
    tr = _reprompt(state, scenario, level, "stall", now_ms)

    # 촉진은 상한 없이 반복된다 — "실패로 끝나는 경로는 만들지 않는다" 는 설계다.
    # 그렇다고 조용히 반복만 하면 아동은 막혀 있는데 **아무도 모른다.** 위계가 상한에
    # 닿은 뒤로도 계속 정체하면 사람을 부른다. 세션은 멈추지 않는다.
    ladder = len(scenario.prompt_hierarchy) or 1
    if stalls >= ladder + STALL_ALERT_AFTER and (stalls - ladder) % STALL_ALERT_AFTER == 0:
        tr = Transition(tr.state, tr.effects + [
            Log("stall_saturated", {"target": state.target(scenario), "stall_count": stalls,
                                    "prompt_level": level.value}),
            Broadcast({"type": "operator_attention", "reason": "stall_saturated",
                       "target": state.target(scenario), "stall_count": stalls},
                      roles=(ev.Role.OPERATOR,)),
        ])
    return tr


def _reprompt(state: SessionState, scenario: Scenario, level: PromptLevel,
              trigger: str, now_ms: int, **fields) -> Transition:
    """지금 항목의 요청 발화를 다시 낸다. WAIT_CHILD 에 머문다.

    REQUEST 로 되돌아가면 반응시간 기준점(`request_at_ms`)이 리셋돼 §7 지표가 망가지고,
    태블릿이 advance 를 한 번 더 보내야 한다.
    """
    target = state.target(scenario)
    if target is None:
        return Transition(state, [Log("reprompt_no_target", {"trigger": trigger})])
    state = replace(state, prompt_level=level)
    # 촉진은 WAIT_CHILD 에 머문 채 발화한다 — 그 구간은 마이크가 열려 있다.
    # 스피커와 마이크가 같은 기기이므로, 닫지 않으면 **로봇이 자기 말을 듣고**
    # 아동 발화로 전사한다. 재생이 끝나면 audio 스포크가 다시 열어 준다.
    effects: list[Effect] = []
    if state.listen_mode != "off":
        state = replace(state, listen_mode="off")
        effects.append(SetListen("off"))
    return Transition(state, effects + [
        Broadcast(
            _state_event(state, scenario, utterance_id=scenario.utterance(target, level.value))
        ),
        Log("prompt_given", {"trigger": trigger, "target": target,
                             "prompt_level": level.value, "retry": state.retry,
                             "stall_count": state.stall_count,
                             "waited_ms": max(0, now_ms - state.request_at_ms), **fields}),
        StartTimer(TIMER_STALL, scenario.stall_timeout_ms),
    ])


def _handle_utterance(
    state: SessionState, event, scenario: Scenario, now_ms: int
) -> Transition:
    """아동이 말했다. 의도만 쓰고 문장은 로그로만 남긴다.

    발화는 **만들지 않는다** — 응답은 전부 사전 녹음된 대본이다. 자유 생성에 맡기면
    아동이 문장을 학습할 수 없고, 로그의 `prompt_level` 이 무의미해져 독립 수행률을
    계산할 수 없다 (§12).
    """
    intent = event.intent
    log = Log("child_utterance", {"intent": intent.value, "text": event.text[:120],
                                  "confidence": event.confidence,
                                  "phase": state.phase.value})

    if intent == ev.Intent.BREAK:
        # 감각 과부하 요구는 어느 상태에서나 존중한다.
        tr = handle(state, ev.Pause(), scenario, now_ms=now_ms)
        return Transition(tr.state, [log] + tr.effects)

    # 나머지 의도는 아동이 물건을 고르는 중일 때만 뜻이 있다.
    if state.phase is not Phase.WAIT_CHILD:
        return Transition(state, [log])

    if intent == ev.Intent.REPEAT_REQUEST:
        # **위계를 올리지 않는다.** 다시 물어보는 것은 난이도 신호가 아니라
        # 작업기억 지표다 (Ran 하위과제 3). 올려 버리면 그 지표가 촉진과 뒤섞인다.
        state = replace(state, repeat_requests=state.repeat_requests + 1)
        tr = _reprompt(state, scenario, state.prompt_level, "repeat_request", now_ms,
                       repeat_requests=state.repeat_requests)
        return Transition(tr.state, [log] + tr.effects)

    if intent == ev.Intent.DONT_KNOW:
        # 이건 난이도 신호다. 정체와 같은 위계를 한 단계 민다.
        stalls = state.stall_count + 1
        level = _prompt_level_for(state.retry + stalls, scenario)
        state = replace(state, stall_count=stalls)
        tr = _reprompt(state, scenario, level, "dont_know", now_ms)
        return Transition(tr.state, [log] + tr.effects)

    # done · other — 기록만 한다. 판정은 감시가 하지 아동의 선언이 하지 않는다.
    return Transition(state, [log])
