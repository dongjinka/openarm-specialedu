"""변화 구동 감시 · 정체 촉진 전이 테스트 (§C).

여기서 지키려는 불변식은 두 가지다.
  1. 감시는 WAIT_CHILD(판정)와 ROBOT_TURN(경고)에서만 돌고, 그 밖에서는 꺼진다.
  2. 아무 변화가 없어도 세션이 정지하지 않는다 — 정체 타이머가 촉진을 올린다.
"""

from __future__ import annotations

from orchestrator import events as ev
from orchestrator.effects import Broadcast, CancelTimer, Log, SetWatch, StartTimer
from orchestrator.machine import TIMER_STALL, SessionState, handle
from orchestrator.scenario import find_scenario
from orchestrator.states import Phase

SC = find_scenario("minsu_playdate_v1")


def step(state, event, t=0):
    return handle(state, event, SC, now_ms=t)


def kinds(effects, cls):
    return [e for e in effects if isinstance(e, cls)]


def log_fields(effects, name):
    return [e.fields for e in kinds(effects, Log) if e.event == name]


def start():
    """IDLE → WAIT_CHILD 까지 몰고 간 상태와 마지막 전이의 효과."""
    s = SessionState()
    for e in (ev.SessionStart(scenario_id=SC.scenario_id), ev.Advance(), ev.Advance()):
        s = step(s, e).state
    tr = step(s, ev.Advance())          # REQUEST -> WAIT_CHILD
    assert tr.state.phase is Phase.WAIT_CHILD
    return tr.state, tr.effects


# ── 감시 모드 ────────────────────────────────────────────────────────────


def test_wait_child_arms_watcher_and_stall_timer():
    state, effects = start()
    assert [w.mode for w in kinds(effects, SetWatch)] == ["judge"]
    timers = [t.name for t in kinds(effects, StartTimer)]
    assert TIMER_STALL in timers
    assert state.watch_mode == "judge"


def test_judge_disarms_watcher_and_cancels_stall():
    state, _ = start()
    tr = step(state, ev.ChildPlaced())
    assert tr.state.phase is Phase.JUDGE
    assert [w.mode for w in kinds(tr.effects, SetWatch)] == ["off"]
    # 판정 중에 감시가 계속 돌면 같은 프레임을 두 번 판정하게 된다.
    assert TIMER_STALL in [c.name for c in kinds(tr.effects, CancelTimer)]


def test_robot_turn_switches_to_guard_mode():
    state, _ = start()
    state = step(state, ev.ChildPlaced()).state
    state = step(state, ev.Judge(object="flower", should_pack=True, confidence=0.9)).state
    assert state.phase is Phase.CORRECT
    tr = step(state, ev.Advance())
    assert tr.state.phase is Phase.ROBOT_TURN
    # guard 는 VLM 을 부르지 않는다. 프레임 차분으로 경고만 낸다.
    assert [w.mode for w in kinds(tr.effects, SetWatch)] == ["guard"]
    assert tr.state.watch_mode == "guard"


def test_watch_mode_never_repeats_itself_across_a_full_item():
    """전이마다 SetWatch 를 남발하면 스포크가 감시를 껐다 켰다 하며 변화를 놓친다.

    한 항목을 오답 1회 포함해 끝까지 돌리고, 나온 모드 열에 **연속 중복이 없는지** 본다.
    """
    state = SessionState()
    modes: list[str] = []

    def go(event, t=0):
        nonlocal state
        tr = step(state, event, t)
        state = tr.state
        modes.extend(w.mode for w in kinds(tr.effects, SetWatch))

    go(ev.SessionStart(scenario_id=SC.scenario_id))
    go(ev.Advance()); go(ev.Advance()); go(ev.Advance())          # -> WAIT_CHILD
    go(ev.ChildPlaced())                                          # -> JUDGE
    go(ev.Judge(object="whale", should_pack=False, confidence=0.9))  # 오답
    go(ev.Advance()); go(ev.Advance())                            # -> WAIT_CHILD
    go(ev.ChildPlaced())
    go(ev.Judge(object="flower", should_pack=True, confidence=0.9))  # 정답
    go(ev.Advance())                                              # -> ROBOT_TURN

    assert modes, "감시 명령이 하나도 나오지 않았다"
    assert all(a != b for a, b in zip(modes, modes[1:])), f"연속 중복: {modes}"
    assert modes[0] == "judge" and modes[-1] == "guard"


# ── 정체 촉진 ────────────────────────────────────────────────────────────


def test_stall_reprompts_without_leaving_wait_child():
    state, _ = start()
    tr = step(state, ev.Timeout(timer=TIMER_STALL), t=SC.stall_timeout_ms)
    # REQUEST 로 되돌아가면 반응시간 기준점이 리셋돼 §7 지표가 망가진다.
    assert tr.state.phase is Phase.WAIT_CHILD
    assert tr.state.request_at_ms == state.request_at_ms

    given = log_fields(tr.effects, "prompt_given")
    assert len(given) == 1 and given[0]["trigger"] == "stall"
    assert given[0]["stall_count"] == 1

    # 발화가 실제로 다시 나가고, 타이머가 다시 걸린다.
    said = [b for b in kinds(tr.effects, Broadcast) if b.payload.get("utterance_id")]
    assert said, "촉진 발화가 나가지 않았다"
    assert TIMER_STALL in [t.name for t in kinds(tr.effects, StartTimer)]


def test_repeated_stalls_climb_the_prompt_ladder_and_stop_at_model():
    state, _ = start()
    seen = []
    for _ in range(5):
        tr = step(state, ev.Timeout(timer=TIMER_STALL))
        state = tr.state
        seen.append(state.prompt_level.value)
    # verbal 에서 시작해 hint → model 로 오르고 model 에 머문다.
    assert seen[0] == "hint" and seen[1] == "model"
    assert seen[-1] == "model", "상한을 넘어도 실패로 끝나는 경로는 없어야 한다"
    assert state.phase is Phase.WAIT_CHILD


def test_stall_and_retry_push_the_same_ladder():
    """오답 1회 뒤에는 정체 1회만으로 model 까지 간다 — 두 신호가 같은 위계를 민다."""
    state, _ = start()
    state = step(state, ev.ChildPlaced()).state
    state = step(state, ev.Judge(object="whale", should_pack=False, confidence=0.9)).state
    assert state.phase is Phase.INCORRECT and state.retry == 1
    state = step(state, ev.Advance()).state          # -> REQUEST
    state = step(state, ev.Advance()).state          # -> WAIT_CHILD
    assert state.prompt_level.value == "hint"        # retry=1
    state = step(state, ev.Timeout(timer=TIMER_STALL)).state
    assert state.prompt_level.value == "model"       # retry=1 + stall=1


def test_stall_count_resets_on_next_item():
    state, _ = start()
    state = step(state, ev.Timeout(timer=TIMER_STALL)).state
    assert state.stall_count == 1
    state = step(state, ev.ChildPlaced()).state
    state = step(state, ev.Judge(object="flower", should_pack=True, confidence=0.9)).state
    assert state.stall_count == 0, "정답을 맞히면 촉진 이력은 다음 항목으로 넘어가지 않는다"


# ── guard 경고 ───────────────────────────────────────────────────────────


def test_zone_disturbed_during_robot_turn_alerts_operator_only():
    state, _ = start()
    state = step(state, ev.ChildPlaced()).state
    state = step(state, ev.Judge(object="flower", should_pack=True, confidence=0.9)).state
    state = step(state, ev.Advance()).state
    assert state.phase is Phase.ROBOT_TURN

    tr = step(state, ev.ZoneDisturbed(detail="배치면 변화"))
    # 팔은 계속 움직인다 — 이송 중 중단은 들고 있던 물건을 떨어뜨린다.
    assert tr.state.phase is Phase.ROBOT_TURN
    alerts = [b for b in kinds(tr.effects, Broadcast)
              if b.payload.get("reason") == "zone_disturbed"]
    assert len(alerts) == 1
    assert alerts[0].roles == (ev.Role.OPERATOR,), "아동 화면에는 띄우지 않는다"
    assert log_fields(tr.effects, "zone_disturbed")


def test_zone_disturbed_outside_robot_turn_is_ignored():
    state, _ = start()
    tr = step(state, ev.ZoneDisturbed(detail="변화"))
    assert log_fields(tr.effects, "zone_disturbed_ignored")
    assert not [b for b in kinds(tr.effects, Broadcast)
                if b.payload.get("reason") == "zone_disturbed"]


# ── 발신자 권한 ──────────────────────────────────────────────────────────


def test_capture_may_trigger_judgement_but_vlm_may_not():
    assert ev.sender_allowed("child_placed", ev.Role.CAPTURE)
    assert ev.sender_allowed("child_placed", ev.Role.OPERATOR)   # 폴백은 유지된다
    assert not ev.sender_allowed("child_placed", ev.Role.VLM)
    assert not ev.sender_allowed("zone_disturbed", ev.Role.VLM)
