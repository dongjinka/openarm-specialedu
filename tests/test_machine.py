"""상태 머신 전이 테스트. 서버도 로봇도 없이 순수 함수만 돌린다."""

from __future__ import annotations

from dataclasses import replace

from orchestrator import events as ev
from orchestrator.effects import Broadcast, CancelTimer, Log, SendRobot, StartTimer
from orchestrator.machine import SessionState, handle
from orchestrator.scenario import find_scenario
from orchestrator.states import Phase

SC = find_scenario("minsu_playdate_v1")


def step(state, event, t=0):
    return handle(state, event, SC, now_ms=t)


def kinds(effects, cls):
    return [e for e in effects if isinstance(e, cls)]


def logs(effects):
    return {e.event for e in kinds(effects, Log)}


def drive(state, events, t=0):
    """이벤트 목록을 순서대로 흘린다. 마지막 상태와 누적 효과를 준다."""
    acc = []
    for e in events:
        tr = step(state, e, t)
        state, _ = tr.state, acc.extend(tr.effects)
    return state, acc


def start() -> SessionState:
    s, _ = drive(SessionState(), [
        ev.SessionStart(scenario_id=SC.scenario_id),
        ev.Advance(),   # INTRO -> SHOW_LIST
        ev.Advance(),   # SHOW_LIST -> REQUEST
        ev.Advance(),   # REQUEST -> WAIT_CHILD
    ])
    return s


# ------------------------------------------------------------------ 골격

def test_bag_setup_phase_is_gone():
    """가방은 운영자가 세션 전에 세팅한다 — 아동 과제에서 제거됐다."""
    assert not hasattr(Phase, "BAG_SETUP")
    assert "BAG_SETUP" not in {p.value for p in Phase}


def test_intro_to_wait_child():
    s = start()
    assert s.phase is Phase.WAIT_CHILD
    assert s.target(SC) == "flower"


def test_session_start_resets_previous_state():
    dirty = SessionState(phase=Phase.IDLE, packed=("tree",), retry=2)
    tr = step(dirty, ev.SessionStart(scenario_id=SC.scenario_id))
    assert tr.state.packed == ()
    assert tr.state.retry == 0


# ------------------------------------------------------------------ 판정

def test_correct_leads_to_robot_turn_and_emits_cmd():
    s = start()
    s, _ = drive(s, [ev.ChildPlaced()])
    assert s.phase is Phase.JUDGE

    tr = step(s, ev.Judge(object="flower", should_pack=True, confidence=0.94), t=4200)
    assert tr.state.phase is Phase.CORRECT
    assert "flower" in tr.state.packed
    resp = [e for e in kinds(tr.effects, Log) if e.event == "child_response"][0]
    assert resp.fields["correct"] is True
    assert resp.fields["latency_ms"] == 4200
    assert resp.fields["on_target"] is True
    assert resp.fields["independent"] is True

    tr2 = step(tr.state, ev.Advance())
    assert tr2.state.phase is Phase.ROBOT_TURN
    cmds = kinds(tr2.effects, SendRobot)
    assert len(cmds) == 1
    assert cmds[0].payload["motion"] == "open_place_close"
    assert cmds[0].payload["cmd_id"] == "c1"
    # 시나리오에서 읽는다 — 데드라인은 데이터셋이 바뀔 때마다 갱신되는 실측값이다
    assert cmds[0].payload["deadline_ms"] == SC.robot_deadline_ms


def test_incorrect_does_not_move_the_arm():
    s = start()
    s, _ = drive(s, [ev.ChildPlaced()])
    tr = step(s, ev.Judge(object="car", should_pack=False, confidence=0.9))
    assert tr.state.phase is Phase.INCORRECT
    assert kinds(tr.effects, SendRobot) == []
    assert tr.state.retry == 1


def test_duplicate_is_distinct_from_incorrect():
    s = start()
    s = replace(s, packed=("flower",))
    s, _ = drive(s, [ev.ChildPlaced()])
    tr = step(s, ev.Judge(object="flower", should_pack=True, confidence=0.9))
    assert tr.state.phase is Phase.DUPLICATE
    assert kinds(tr.effects, SendRobot) == []


def test_operator_override_is_an_ordinary_event():
    s = start()
    s, _ = drive(s, [ev.ChildPlaced()])
    tr = step(s, ev.JudgeOverride(object="flower", should_pack=True))
    assert tr.state.phase is Phase.CORRECT
    judge = [e for e in kinds(tr.effects, Log) if e.event == "vlm_judge"][0]
    assert judge.fields["overridden"] is True


def test_judge_timeout_holds_and_calls_operator():
    """§5 — confidence 가 낮거나 응답이 없으면 자동 진행하지 않는다."""
    s = start()
    s, _ = drive(s, [ev.ChildPlaced()])
    tr = step(s, ev.Timeout(timer="judge"))
    assert tr.state.phase is Phase.JUDGE
    assert "judge_timeout" in logs(tr.effects)
    assert any(b.payload.get("type") == "operator_attention" for b in kinds(tr.effects, Broadcast))


# ------------------------------------------------------- 프롬프트 위계

def test_prompt_level_escalates_and_never_dead_ends():
    s = start()
    for expected in ("hint", "model", "model", "model"):
        s, _ = drive(s, [ev.ChildPlaced()])
        s, _ = drive(s, [ev.Judge(object="car", should_pack=False, confidence=0.9)])
        assert s.phase is Phase.INCORRECT
        s, _ = drive(s, [ev.Advance()])       # -> REQUEST
        assert s.phase is Phase.REQUEST, "실패로 끝나는 경로가 있으면 안 된다"
        assert s.prompt_level.value == expected
        s, _ = drive(s, [ev.Advance()])       # -> WAIT_CHILD


def test_retry_resets_after_a_correct_answer():
    s = start()
    s, _ = drive(s, [ev.ChildPlaced(), ev.Judge(object="car", should_pack=False, confidence=0.9)])
    assert s.retry == 1
    s, _ = drive(s, [ev.Advance(), ev.Advance(), ev.ChildPlaced(),
                     ev.Judge(object="flower", should_pack=True, confidence=0.9)])
    assert s.retry == 0


# ------------------------------------------------------------ 로봇 턴

def reach_robot_turn(obj="flower"):
    s = start()
    s, _ = drive(s, [ev.ChildPlaced(),
                     ev.Judge(object=obj, should_pack=True, confidence=0.95),
                     ev.Advance()])
    assert s.phase is Phase.ROBOT_TURN
    return s


def test_robot_done_goes_to_verify_not_next():
    s = reach_robot_turn()
    tr = step(s, ev.RobotDone(cmd_id="c1", success=True,
                              reason=ev.DoneReason.VERIFIED, duration_ms=30800))
    assert tr.state.phase is Phase.ROBOT_VERIFY


def test_stale_cmd_id_is_ignored():
    """지연된 완료 신호가 다음 명령과 섞이면 안 된다 (§4.4)."""
    s = reach_robot_turn()
    tr = step(s, ev.RobotDone(cmd_id="c99", success=True,
                              reason=ev.DoneReason.VERIFIED, duration_ms=1))
    assert tr.state.phase is Phase.ROBOT_TURN
    assert "stale_robot_done" in logs(tr.effects)


def test_robot_timeout_still_verifies():
    """타임아웃이어도 물건이 들어갔을 수 있다 — 실패로 단정하지 않는다."""
    s = reach_robot_turn()
    tr = step(s, ev.Timeout(timer="robot", cmd_id="c1"), t=SC.robot_deadline_ms)
    assert tr.state.phase is Phase.ROBOT_VERIFY
    aborts = [e for e in kinds(tr.effects, SendRobot) if e.payload["type"] == "robot_abort"]
    assert aborts and aborts[0].payload["reason"] == "timeout"


def test_robot_error_goes_to_robot_fail_with_recovery_utterance():
    s = reach_robot_turn()
    tr = step(s, ev.RobotError(cmd_id="c1", reason=ev.FailureReason.GRASP_FAILED, detail="미끄러짐"))
    assert tr.state.phase is Phase.ROBOT_FAIL
    said = [b.payload.get("utterance_id") for b in kinds(tr.effects, Broadcast)]
    assert "robot_mistake" in said, "로봇의 실패를 아동이 자기 실패로 받으면 안 된다"


def test_progress_tick_reaches_the_tablet():
    """무발화 30초 구간의 비언어적 지속 신호."""
    s = reach_robot_turn()
    tr = step(s, ev.ProgressTick(cmd_id="c1", elapsed_ms=8000))
    assert any(b.payload.get("type") == "progress_tick" for b in kinds(tr.effects, Broadcast))
    tr2 = step(s, ev.ProgressTick(cmd_id="c-old", elapsed_ms=1))
    assert "stale_progress" in logs(tr2.effects)


# --------------------------------------------------------------- 사후 확인

def test_verify_success_advances():
    s = reach_robot_turn()
    s, _ = drive(s, [ev.RobotDone(cmd_id="c1", success=True,
                                  reason=ev.DoneReason.VERIFIED, duration_ms=30800)])
    tr = step(s, ev.VerifyResult(cmd_id="c1", object_in_bag=True, bag_closed=True))
    assert tr.state.phase is Phase.REQUEST      # NEXT 를 지나 다음 항목으로
    assert tr.state.packed == ("flower",)
    assert tr.state.pending_cmd_id is None


def test_verify_failure_rolls_back_packed():
    """사후 확인이 실패하면 담았다고 표시했던 것을 되돌린다."""
    s = reach_robot_turn()
    s, _ = drive(s, [ev.RobotDone(cmd_id="c1", success=True,
                                  reason=ev.DoneReason.VERIFIED, duration_ms=30800)])
    assert "flower" in s.packed
    tr = step(s, ev.VerifyResult(cmd_id="c1", object_in_bag=False, bag_closed=True))
    assert tr.state.phase is Phase.ROBOT_FAIL
    assert "flower" not in tr.state.packed


def test_bag_left_open_is_a_failure():
    """다음 턴이 '가방 닫힘'에서 시작하는 전제를 지킨다."""
    s = reach_robot_turn()
    s, _ = drive(s, [ev.RobotDone(cmd_id="c1", success=True,
                                  reason=ev.DoneReason.VERIFIED, duration_ms=30800)])
    tr = step(s, ev.VerifyResult(cmd_id="c1", object_in_bag=True, bag_closed=False))
    assert tr.state.phase is Phase.ROBOT_FAIL
    assert tr.state.bag_ok is False


def test_verify_timeout_fails_safe():
    s = reach_robot_turn()
    s, _ = drive(s, [ev.RobotDone(cmd_id="c1", success=True,
                                  reason=ev.DoneReason.VERIFIED, duration_ms=30800)])
    tr = step(s, ev.Timeout(timer="verify"))
    assert tr.state.phase is Phase.ROBOT_FAIL


def test_robot_fail_can_skip_forward():
    s = reach_robot_turn()
    s, _ = drive(s, [ev.RobotError(cmd_id="c1", reason=ev.FailureReason.UNKNOWN)])
    tr = step(s, ev.Advance())
    assert tr.state.phase is Phase.REQUEST


# ------------------------------------------------------------------ 안전

def test_camera_loss_blocks_a_new_robot_turn():
    """카메라 3대 전부가 정책 필수 입력이다."""
    s = start()
    s, _ = drive(s, [ev.CameraHealth(cameras={"left_wrist": False}, all_ok=False),
                     ev.ChildPlaced(),
                     ev.Judge(object="flower", should_pack=True, confidence=0.9)])
    tr = step(s, ev.Advance())
    assert tr.state.phase is Phase.ROBOT_FAIL
    assert kinds(tr.effects, SendRobot) == []
    assert "robot_blocked" in logs(tr.effects)


def test_camera_loss_mid_motion_aborts():
    s = reach_robot_turn()
    tr = step(s, ev.CameraHealth(cameras={"right_wrist": False}, all_ok=False))
    assert tr.state.phase is Phase.ROBOT_FAIL
    assert any(e.payload["type"] == "robot_abort" for e in kinds(tr.effects, SendRobot))


def test_pause_during_motion_stops_the_arm():
    s = reach_robot_turn()
    tr = step(s, ev.Pause())
    assert tr.state.phase is Phase.PAUSED
    assert any(e.payload["type"] == "robot_abort" for e in kinds(tr.effects, SendRobot))
    assert any(isinstance(e, CancelTimer) for e in tr.effects)


def test_resume_from_motion_restarts_the_turn():
    """중간부터 잇지 않는다 — CORRECT 로 돌아가 턴을 새로 낸다."""
    s = reach_robot_turn()
    s, _ = drive(s, [ev.Pause()])
    tr = step(s, ev.Resume())
    assert tr.state.phase is Phase.CORRECT


def test_pause_and_resume_elsewhere_returns_in_place():
    s = start()
    s, _ = drive(s, [ev.Pause()])
    assert s.phase is Phase.PAUSED
    tr = step(s, ev.Resume())
    assert tr.state.phase is Phase.WAIT_CHILD


def test_operator_abort_goes_to_verify():
    s = reach_robot_turn()
    tr = step(s, ev.RobotAbort(cmd_id="c1", reason="operator"))
    assert tr.state.phase is Phase.ROBOT_VERIFY


def test_contact_anomaly_is_logged_without_changing_phase():
    s = reach_robot_turn()
    tr = step(s, ev.ContactAnomaly(joint="left_4", deviation=12.3))
    assert tr.state.phase is Phase.ROBOT_TURN
    assert "contact_anomaly" in logs(tr.effects)


# ------------------------------------------------------------------ 기타

def test_force_state_cannot_reach_paused():
    s = start()
    tr = step(s, ev.ForceState(phase="PAUSED"))
    assert tr.state.phase is Phase.WAIT_CHILD
    assert "force_state_rejected" in logs(tr.effects)


def test_force_state_jumps():
    s = start()
    tr = step(s, ev.ForceState(phase="REQUEST"))
    assert tr.state.phase is Phase.REQUEST


def test_unknown_event_for_phase_is_ignored_not_crashing():
    s = start()
    tr = step(s, ev.RobotDone(cmd_id="c1", success=True,
                              reason=ev.DoneReason.VERIFIED, duration_ms=1))
    assert tr.state.phase is Phase.WAIT_CHILD
    assert "event_ignored" in logs(tr.effects)


def test_vlm_cannot_send_robot_events():
    """§12 — VLM → VLA 직접 호출 경로는 타입 수준에서 존재하지 않는다."""
    assert ev.sender_allowed("judge", ev.Role.VLM)
    for forbidden in ("robot_done", "robot_abort", "robot_error", "force_state", "session_start"):
        assert not ev.sender_allowed(forbidden, ev.Role.VLM), forbidden


def test_full_three_item_session_completes():
    s = start()
    for i, obj in enumerate(("flower", "whale", "tree"), start=1):
        s, _ = drive(s, [ev.ChildPlaced(),
                         ev.Judge(object=obj, should_pack=True, confidence=0.95),
                         ev.Advance()])
        assert s.phase is Phase.ROBOT_TURN
        assert s.pending_cmd_id == f"c{i}"
        s, _ = drive(s, [ev.RobotDone(cmd_id=f"c{i}", success=True,
                                      reason=ev.DoneReason.VERIFIED, duration_ms=30800),
                         ev.VerifyResult(cmd_id=f"c{i}", object_in_bag=True, bag_closed=True)])
        if i < 3:
            s, _ = drive(s, [ev.Advance()])   # REQUEST -> WAIT_CHILD
    assert s.phase is Phase.COMPLETE
    assert set(s.packed) == {"flower", "whale", "tree"}
    tr = step(s, ev.Advance())
    assert tr.state.phase is Phase.END


def test_tablet_highlights_the_item_being_packed_not_the_next_one():
    """CORRECT 에서 ✓ 가 다음 항목에 붙으면 아동이 잘못된 대응을 학습한다."""
    s = start()
    s, _ = drive(s, [ev.ChildPlaced()])
    tr = step(s, ev.Judge(object="flower", should_pack=True, confidence=0.95))
    correct = [b for b in kinds(tr.effects, Broadcast) if b.payload.get("phase") == "CORRECT"][0]
    assert correct.payload["target"] == "flower"
    assert correct.payload["progress"]["packed"] == ["flower"]

    s2, _ = drive(tr.state, [ev.Advance(),
                             ev.RobotDone(cmd_id="c1", success=True,
                                          reason=ev.DoneReason.VERIFIED, duration_ms=30800)])
    tr2 = step(s2, ev.ProgressTick(cmd_id="c1", elapsed_ms=1))  # phase 유지 확인용
    assert s2.phase is Phase.ROBOT_VERIFY and s2.pending_object == "flower"


def test_vlm_hold_does_not_decide_and_calls_the_operator():
    """§5 — 신뢰도가 낮으면 VLM 이 추측하지 않는다. 로봇은 움직이지 않는다."""
    s = start()
    s, _ = drive(s, [ev.ChildPlaced()])
    tr = step(s, ev.VlmHold(object="other", confidence=0.31, reason="가려짐"))
    assert tr.state.phase is Phase.JUDGE, "판정 없이 JUDGE 에 머문다"
    assert kinds(tr.effects, SendRobot) == []
    assert "vlm_hold" in logs(tr.effects)
    assert any(b.payload.get("reason") == "vlm_hold" for b in kinds(tr.effects, Broadcast))

    # 운영자가 확정하면 정상 흐름으로 복귀한다
    tr2 = step(tr.state, ev.JudgeOverride(object="flower", should_pack=True))
    assert tr2.state.phase is Phase.CORRECT


def test_vlm_cannot_send_vlm_hold_as_another_role():
    assert ev.sender_allowed("vlm_hold", ev.Role.VLM)
    assert not ev.sender_allowed("vlm_hold", ev.Role.ROBOT)
