"""VLM 지각 → 판정 규칙. 모델 호출 없이 검증한다."""

from __future__ import annotations

import pytest

from vlm_service.backends.base import parse_perception
from vlm_service.contract import Judgment, ObjectClass, Perception, decide

CHECK = ["flower", "whale", "tree"]


def perceive(obj: str, conf: float = 0.95) -> Perception:
    return Perception(object=ObjectClass(obj), confidence=conf)


# ------------------------------------------------------------- 파싱

def test_parses_plain_json():
    p = parse_perception('{"object":"tree","confidence":0.91,"reason":"green on tan trunk"}')
    assert p.object is ObjectClass.TREE and p.confidence == 0.91


def test_survives_code_fences_and_prose():
    raw = 'Sure!\n```json\n{"object":"whale","confidence":0.8,"reason":"blue"}\n```\n'
    assert parse_perception(raw).object is ObjectClass.WHALE


@pytest.mark.parametrize("raw", ["", "no json here", "{broken", "{}"])
def test_unparseable_falls_back_to_none_not_a_guess(raw):
    """추측하지 않고 사람에게 넘긴다."""
    p = parse_perception(raw)
    assert p.object is ObjectClass.NONE
    assert p.confidence == 0.0


def test_unknown_class_becomes_other_not_a_checklist_item():
    p = parse_perception('{"object":"duck","confidence":0.9}')
    assert p.object is ObjectClass.OTHER, "모르는 물건이 체크리스트 물건이 되면 안 된다"


def test_confidence_is_clamped():
    assert parse_perception('{"object":"tree","confidence":5}').confidence == 1.0
    assert parse_perception('{"object":"tree","confidence":-1}').confidence == 0.0
    assert parse_perception('{"object":"tree","confidence":"높음"}').confidence == 0.0


# ------------------------------------------------------------- 판정

def test_checklist_item_should_pack():
    j = decide(perceive("tree"), CHECK, [])
    assert j.should_pack and not j.hold_for_operator


def test_already_packed_is_not_repacked():
    assert decide(perceive("tree"), CHECK, ["tree"]).should_pack is False


def test_distractor_never_triggers_the_arm():
    """방해자극이 체크리스트 물건으로 새면 로봇이 엉뚱한 물건을 집는다."""
    j = decide(perceive("other", 0.99), CHECK, [])
    assert j.should_pack is False
    assert j.hold_for_operator is True


def test_empty_surface_holds_for_operator():
    j = decide(perceive("none", 0.99), CHECK, [])
    assert j.should_pack is False and j.hold_for_operator is True


def test_low_confidence_holds_even_when_class_is_right():
    """§5 — confidence 가 낮으면 판정 보류, 사람이 확정."""
    j = decide(perceive("tree", 0.42), CHECK, [], min_confidence=0.70)
    assert j.should_pack is True
    assert j.hold_for_operator is True, "낮은 신뢰도는 사람에게 넘어가야 한다"


def test_judgment_matches_the_orchestrator_event_shape():
    """§4.4 의 judge 이벤트로 그대로 실릴 수 있어야 한다."""
    from orchestrator.events import Judge

    j: Judgment = decide(perceive("whale", 0.88), CHECK, ["flower"])
    Judge(object=j.object, should_pack=j.should_pack, confidence=j.confidence)


def test_confidence_threshold_is_not_the_real_safety_net():
    """실측에서 유일한 오답의 confidence 가 0.88 이었다.

    임계값으로는 그 오답을 못 거른다. 실제 안전망은 `none`/`other` 분류다 —
    그래서 decide() 는 confidence 와 무관하게 이 두 클래스를 보류시킨다.
    """
    high_conf_but_unsure = decide(perceive("none", 0.99), CHECK, [], min_confidence=0.5)
    assert high_conf_but_unsure.hold_for_operator is True
    assert high_conf_but_unsure.should_pack is False
