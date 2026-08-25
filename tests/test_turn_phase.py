"""로봇 턴 하위 단계 표시 (§G).

로봇 턴은 **발화가 금지**된 46초 구간이다(실행은 더 길 수 있다). `progress_tick` 은
스톱워치일 뿐이라 아동에게 "얼마나 남았나"를 못 알려준다. 경계는 60에피소드의
그리퍼 채널에서 뽑았고, 표준편차가 0.019~0.029 로 매우 일관적이다.

**시간 기준 근사다.** 정책이 멈추거나 느려지면 실제 동작과 어긋난다 — 표시용이고
성공 판정용이 아니다. 성공은 ROBOT_VERIFY 의 VLM 이 정한다.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from orchestrator import events as ev
from orchestrator.effects import Broadcast, Log
from orchestrator.machine import SessionState, handle
from orchestrator.scenario import find_scenario
from orchestrator.states import Phase

SC = find_scenario("minsu_playdate_v1")


def turning():
    return replace(SessionState(), phase=Phase.ROBOT_TURN, pending_cmd_id="c1")


def tick(state, elapsed_ms):
    tr = handle(state, ev.ProgressTick(cmd_id="c1", elapsed_ms=elapsed_ms), SC)
    payloads = [e.payload for e in tr.effects if isinstance(e, Broadcast)]
    return payloads[0] if payloads else None


@pytest.mark.parametrize(("ratio", "expected"), [
    (0.00, "opening"), (0.26, "opening"),
    (0.28, "reaching"), (0.51, "reaching"),
    (0.53, "placing"), (0.91, "placing"),
    (0.93, "closing"), (1.00, "closing"),
])
def test_phase_boundaries_follow_the_measured_gripper_transitions(ratio, expected):
    assert SC.turn_phase(ratio) == expected


def test_overrun_stays_on_the_last_phase():
    """데드라인을 넘겨도 단계가 사라지면 안 된다 — 화면이 빈 채로 남는다."""
    assert SC.turn_phase(1.8) == "closing"


def test_progress_tick_carries_ratio_and_phase():
    state = turning()
    # 0.5 는 reaching 이다 — 물체 집기 경계가 0.517 이라 딱 걸친다. 0.7 로 확실히 넘긴다.
    payload = tick(state, int(SC.robot_deadline_ms * 0.7))
    assert payload["type"] == "progress_tick"
    assert payload["ratio"] == pytest.approx(0.7, abs=0.01)
    assert payload["turn_phase"] == "placing"

    half = tick(state, SC.robot_deadline_ms // 2)
    assert half["turn_phase"] == "reaching"


def test_ratio_is_clamped_so_the_bar_never_overflows():
    payload = tick(turning(), SC.robot_deadline_ms * 3)
    assert payload["ratio"] == 1.0


def test_stale_tick_is_dropped_not_broadcast():
    """지연된 완료 신호가 다음 명령과 섞이는 것을 막는 세대 가드는 여기도 적용된다."""
    tr = handle(turning(), ev.ProgressTick(cmd_id="c99", elapsed_ms=1000), SC)
    assert not [e for e in tr.effects if isinstance(e, Broadcast)]
    assert [e for e in tr.effects if isinstance(e, Log) and e.event == "stale_progress"]


def test_scenario_without_phases_simply_omits_the_label():
    """단계 설정이 없는 시나리오도 그대로 돌아야 한다 — 가산적인 기능이다."""
    bare = replace(SC, turn_phases=())
    assert bare.turn_phase(0.5) is None
    tr = handle(turning(), ev.ProgressTick(cmd_id="c1", elapsed_ms=1000), bare)
    payload = [e.payload for e in tr.effects if isinstance(e, Broadcast)][0]
    assert "turn_phase" not in payload
    assert "ratio" in payload


def test_verify_timeout_outlasts_the_retry_loop():
    """사후 확인 타임아웃이 재시도 루프보다 길어야 한다.

    가방 입구가 팔에 가리면 vlm_service 가 다시 찍는다. 그 루프가 타임아웃을 넘기면
    **로봇이 성공해도** ROBOT_FAIL 로 가고 아동에게 "로봇의 실수" 로 안내된다.
    실측(gpt-4o) 최대 지연 2.63초 기준으로 계산한다.
    """
    from vlm_service.main import VERIFY_RETRIES, VERIFY_RETRY_DELAY_S

    worst_call_s = 2.63          # eval/results_60epi_gpt4o.json 의 최대 지연
    worst_s = (VERIFY_RETRIES + 1) * worst_call_s + VERIFY_RETRIES * VERIFY_RETRY_DELAY_S
    assert SC.verify_timeout_ms / 1000 > worst_s, (
        f"재시도 루프 최악 {worst_s:.1f}초 > 타임아웃 {SC.verify_timeout_ms/1000}초")
