"""VLM 서비스 계약 (§5).

    입력: 씬 카메라 프레임 1장 + 체크리스트(주입) + 이미 담은 항목
    출력(JSON only): {"object": ..., "should_pack": bool, "confidence": 0.0-1.0}

`should_pack` 을 모델이 직접 정하게 두지 않는다. 모델은 **무엇이 보이는지만** 말하고,
체크리스트 대조는 코드가 한다 — 모델이 규칙을 매번 다시 해석하면 재현성이 없다.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ObjectClass(StrEnum):
    TREE = "tree"
    FLOWER = "flower"
    WHALE = "whale"
    #: 체크리스트에 없는 물건(방해자극). 이걸 셋 중 하나로 우겨넣으면
    #: 로봇이 엉뚱한 물건을 집는다 — 3-class 가 아니라 4-class 로 평가하는 이유.
    OTHER = "other"
    #: 배치 구역이 비어 있다.
    NONE = "none"


class Perception(BaseModel):
    """모델이 내놓는 것. 판정이 아니라 지각이다."""

    model_config = ConfigDict(extra="forbid")

    object: ObjectClass
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""


class Judgment(BaseModel):
    """오케스트레이터로 나가는 것 (§4.4 의 `judge` 이벤트)."""

    model_config = ConfigDict(extra="forbid")

    object: str
    should_pack: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""
    #: 신뢰도가 낮거나 판정이 모호하면 True — 운영자 콘솔이 사람에게 넘긴다.
    hold_for_operator: bool = False


def decide(
    perception: Perception,
    checklist: list[str],
    packed: list[str],
    *,
    min_confidence: float = 0.70,
) -> Judgment:
    """지각 → 판정. 규칙은 코드가 소유한다.

    `should_pack` = 체크리스트에 있고 && 아직 담기지 않음.
    """
    obj = perception.object.value
    unpacked = [o for o in checklist if o not in packed]
    should_pack = obj in unpacked

    hold = (
        perception.confidence < min_confidence
        or perception.object in (ObjectClass.NONE, ObjectClass.OTHER)
    )
    return Judgment(
        object=obj,
        should_pack=should_pack,
        confidence=perception.confidence,
        reason=perception.reason,
        hold_for_operator=hold,
    )


class BagState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    #: 팔·그리퍼가 입구를 가려 판정할 수 없다. **추측보다 안전하다** —
    #: `open` 오답은 성공한 턴을 ROBOT_FAIL 로 보내 아동에게 로봇 실수를 알린다.
    HIDDEN = "hidden"


class VerifyPerception(BaseModel):
    """사후 확인에서 모델이 내놓는 것. 판정이 아니라 지각이다."""

    model_config = ConfigDict(extra="forbid")

    surface: ObjectClass
    bag: BagState
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""


class VerifyJudgment(BaseModel):
    """오케스트레이터로 나가는 `verify_result` (§4.4)."""

    model_config = ConfigDict(extra="forbid")

    object_in_bag: bool
    bag_closed: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""
    #: 가방 상태를 못 봤거나 신뢰도가 낮으면 True — 운영자가 확정한다.
    hold_for_operator: bool = False


def decide_verify(
    perception: VerifyPerception, *, min_confidence: float = 0.70
) -> VerifyJudgment:
    """지각 → 사후 판정. 규칙은 코드가 소유한다 (`decide` 와 같은 원칙).

    `object_in_bag` 과 `bag_closed` 는 **서로 다른 신호**다. 예전 구현은 배치면이
    비었다는 사실 하나를 두 필드에 모두 넣어서, 가방이 열린 채 끝난 턴을
    성공으로 기록했다 — 다음 턴의 시작 상태가 분포 밖이 되는데도.
    """
    surface_clear = perception.surface is ObjectClass.NONE

    if perception.bag is BagState.HIDDEN:
        # 판정하지 않는다. 가방이 닫혔다고 추측하면 열린 채 다음 턴이 시작되고,
        # 열렸다고 추측하면 성공한 턴이 로봇 실수로 안내된다. 둘 다 나쁘다.
        return VerifyJudgment(
            object_in_bag=surface_clear, bag_closed=False,
            confidence=perception.confidence, reason=perception.reason,
            hold_for_operator=True,
        )

    return VerifyJudgment(
        object_in_bag=surface_clear,
        bag_closed=perception.bag is BagState.CLOSED,
        confidence=perception.confidence,
        reason=perception.reason,
        hold_for_operator=perception.confidence < min_confidence,
    )
