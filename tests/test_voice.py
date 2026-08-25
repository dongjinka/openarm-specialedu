"""아동 발화 — 의도 분류와 상태 머신 반응 (§E).

STT 는 **가산적**이다. 이 이벤트가 하나도 안 와도 세션은 완결된다. 그래서 여기서
지키려는 것은 "말이 세션을 앞지르지 않는다" 이다 — 판정은 감시가 하지 아동의 선언이 하지 않는다.
"""

from __future__ import annotations

import pytest

from orchestrator import events as ev
from orchestrator.effects import Broadcast, Log, SetListen
from orchestrator.machine import SessionState, handle
from orchestrator.scenario import find_scenario
from orchestrator.states import Phase
from voice_service.intents import classify

SC = find_scenario("minsu_playdate_v1")


def step(state, event, t=0):
    return handle(state, event, SC, now_ms=t)


def kinds(effects, cls):
    return [e for e in effects if isinstance(e, cls)]


def log_fields(effects, name):
    return [e.fields for e in kinds(effects, Log) if e.event == name]


def wait_child():
    s = SessionState()
    for e in (ev.SessionStart(scenario_id=SC.scenario_id), ev.Advance(), ev.Advance()):
        s = step(s, e).state
    tr = step(s, ev.Advance())
    assert tr.state.phase is Phase.WAIT_CHILD
    return tr.state, tr.effects


def said(text: str):
    d = classify(text)
    return ev.ChildUtterance(text=text, intent=d.intent, confidence=d.confidence)


# ── 의도 분류 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("text", "expected"), [
    ("뭐라고?", ev.Intent.REPEAT_REQUEST),
    ("다시 말해줘", ev.Intent.REPEAT_REQUEST),
    ("한 번 더 말해 주세요", ev.Intent.REPEAT_REQUEST),
    ("모르겠어요", ev.Intent.DONT_KNOW),
    ("어디 있어?", ev.Intent.DONT_KNOW),
    ("쉬고 싶어", ev.Intent.BREAK),
    ("그만할래", ev.Intent.BREAK),
    ("다 했어!", ev.Intent.DONE),
    ("고래 가져왔어", ev.Intent.DONE),
])
def test_rule_matching(text, expected):
    assert classify(text).intent is expected


@pytest.mark.parametrize(("text", "expected"), [
    ("머라고?", ev.Intent.REPEAT_REQUEST),
    ("모르게써", ev.Intent.DONT_KNOW),
    ("쉬고시퍼", ev.Intent.BREAK),
    ("다핻어", ev.Intent.DONE),
])
def test_fuzzy_recovers_misrecognition(text, expected):
    """전사가 틀려도 분류가 맞으면 시스템은 정상이다 (stt-eval 의 결론)."""
    d = classify(text)
    assert d.intent is expected and d.how == "fuzzy"


@pytest.mark.parametrize("text", [
    "꽃이 예뻐요", "나무 좋아", "고래는 파란색", "민수야 안녕", "책상 위에 있어", "",
])
def test_scenario_chatter_is_not_forced_into_an_intent(text):
    """추측하지 않는다. 틀린 의도는 아동이 원치 않는 상태 전이를 만든다."""
    assert classify(text).intent is ev.Intent.OTHER


# ── 마이크 창 ────────────────────────────────────────────────────────────


def test_microphone_opens_only_while_the_child_may_speak():
    state, effects = wait_child()
    assert [x.mode for x in kinds(effects, SetListen)] == ["open"]
    # 판정에 들어가면 닫는다 — 로봇이 곧 말하고, 그 다음엔 팔이 돈다.
    tr = step(state, ev.ChildPlaced())
    assert [x.mode for x in kinds(tr.effects, SetListen)] == ["off"]


def test_microphone_stays_shut_through_the_robot_turn():
    """모터 소음 1~2kHz +13dB 구간에서 열면 그 소음을 발화로 오인한다."""
    state, _ = wait_child()
    state = step(state, ev.ChildPlaced()).state
    state = step(state, ev.Judge(object="flower", should_pack=True, confidence=0.9)).state
    tr = step(state, ev.Advance())
    assert tr.state.phase is Phase.ROBOT_TURN
    assert tr.state.listen_mode == "off"


# ── 의도에 대한 반응 ──────────────────────────────────────────────────────


def test_repeat_request_replays_without_climbing_the_ladder():
    """다시 물어보는 것은 난이도 신호가 아니라 작업기억 지표다 (Ran 하위과제 3).

    위계를 올려 버리면 그 지표가 촉진과 뒤섞여 사후 분해가 불가능해진다.
    """
    state, _ = wait_child()
    before = state.prompt_level
    tr = step(state, said("뭐라고?"))
    assert tr.state.prompt_level == before, "재호명에 촉진을 올렸다"
    assert tr.state.repeat_requests == 1
    given = log_fields(tr.effects, "prompt_given")
    assert given and given[0]["trigger"] == "repeat_request"
    # 발화가 실제로 다시 나간다.
    assert [b for b in kinds(tr.effects, Broadcast) if b.payload.get("utterance_id")]


def test_dont_know_climbs_the_ladder():
    state, _ = wait_child()
    assert state.prompt_level.value == "verbal"
    tr = step(state, said("모르겠어"))
    assert tr.state.prompt_level.value == "hint"
    assert log_fields(tr.effects, "prompt_given")[0]["trigger"] == "dont_know"


def test_break_pauses_from_anywhere():
    state, _ = wait_child()
    state = step(state, ev.ChildPlaced()).state
    state = step(state, ev.Judge(object="flower", should_pack=True, confidence=0.9)).state
    state = step(state, ev.Advance()).state
    assert state.phase is Phase.ROBOT_TURN
    tr = step(state, said("그만할래"))
    assert tr.state.phase is Phase.PAUSED, "감각 과부하 요구는 어디서나 존중한다"


def test_done_is_logged_but_never_triggers_a_judgement():
    """판정은 감시가 한다. 아동이 '다 했어' 라고 해서 판정하면,
    아직 물건을 안 올렸는데 빈 배치면을 판정하게 된다."""
    state, _ = wait_child()
    tr = step(state, said("다 했어"))
    assert tr.state.phase is Phase.WAIT_CHILD
    assert log_fields(tr.effects, "child_utterance")[0]["intent"] == "done"
    assert not log_fields(tr.effects, "prompt_given")


def test_utterance_outside_wait_child_is_logged_only():
    state, _ = wait_child()
    state = step(state, ev.ChildPlaced()).state          # JUDGE
    tr = step(state, said("모르겠어"))
    assert tr.state.phase is Phase.JUDGE
    assert log_fields(tr.effects, "child_utterance")
    assert not log_fields(tr.effects, "prompt_given")


def test_voice_role_cannot_reach_the_robot():
    for forbidden in ("robot_cmd", "robot_abort", "judge", "child_placed", "session_start"):
        assert not ev.sender_allowed(forbidden, ev.Role.VOICE)
    assert ev.sender_allowed("child_utterance", ev.Role.VOICE)


# ── 에코: 재생 중에는 듣지 않는다 ────────────────────────────────────────


def test_reprompt_closes_the_microphone_then_advance_reopens_it():
    """스피커와 마이크가 같은 기기다.

    촉진은 WAIT_CHILD 에 머문 채 발화하는데, 그 구간은 마이크가 열려 있다.
    닫지 않으면 **로봇이 자기 말을 듣고** 아동 발화로 전사한다.
    그리고 다시 열지 않으면 한 번 촉진한 뒤로 아동의 말이 영영 안 들린다.
    """
    state, _ = wait_child()
    assert state.listen_mode == "open"

    tr = step(state, said("뭐라고?"))                      # 촉진 재생 시작
    assert tr.state.listen_mode == "off", "재생 중에 마이크가 열려 있다 — 에코가 난다"
    assert [x.mode for x in kinds(tr.effects, SetListen)] == ["off"]

    back = step(tr.state, ev.Advance(from_phase="WAIT_CHILD"))   # 재생 끝
    assert back.state.phase is Phase.WAIT_CHILD, "advance 가 상태를 넘겨서는 안 된다"
    assert back.state.listen_mode == "open"
    assert [x.mode for x in kinds(back.effects, SetListen)] == ["open"]


def test_advance_while_already_listening_is_a_no_op():
    state, _ = wait_child()
    tr = step(state, ev.Advance(from_phase="WAIT_CHILD"))
    assert tr.effects == [] and tr.state.phase is Phase.WAIT_CHILD


def test_stall_prompt_also_closes_the_microphone():
    from orchestrator.machine import TIMER_STALL

    state, _ = wait_child()
    tr = step(state, ev.Timeout(timer=TIMER_STALL))
    assert tr.state.listen_mode == "off"
