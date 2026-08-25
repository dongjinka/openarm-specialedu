"""상태 정의와 화면 표현 테이블.

CLAUDE_CODE_CONTEXT.md §4.5 의 표를 그대로 옮기되 두 곳을 고쳤다:

  - `BAG_SETUP` 삭제 — 가방은 운영자가 세션 전에 세팅한다. 아동 과제는
    "요청받은 물건을 책상에 올리기" 하나로 축소된다.
  - `ROBOT_PACK` → `ROBOT_TURN` 개칭 + `ROBOT_VERIFY` / `ROBOT_FAIL` / `PAUSED` 신설.
    한 번의 호출이 '넣기'가 아니라 '열기+넣기+닫기' 전체(≈46.3초)라서 이름을 맞췄다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from orchestrator.events import Expression


class Phase(StrEnum):
    IDLE = "IDLE"
    INTRO = "INTRO"
    SHOW_LIST = "SHOW_LIST"
    REQUEST = "REQUEST"
    WAIT_CHILD = "WAIT_CHILD"
    JUDGE = "JUDGE"
    CORRECT = "CORRECT"
    INCORRECT = "INCORRECT"
    DUPLICATE = "DUPLICATE"
    ROBOT_TURN = "ROBOT_TURN"
    ROBOT_VERIFY = "ROBOT_VERIFY"
    ROBOT_FAIL = "ROBOT_FAIL"
    NEXT = "NEXT"
    COMPLETE = "COMPLETE"
    END = "END"
    PAUSED = "PAUSED"


@dataclass(frozen=True)
class Presentation:
    """상태가 태블릿에 어떻게 보이는지. 발화·표정은 고정 — VLM 생성 금지 (§5)."""

    expression: Expression
    utterance_id: str | None
    silent: bool = False


#: 발화는 `utterance_id` 로만 나간다 → 사전 녹음 파일 재생 (§4.4).
PRESENTATION: dict[Phase, Presentation] = {
    Phase.IDLE: Presentation(Expression.NEUTRAL, None, silent=True),
    Phase.INTRO: Presentation(Expression.HAPPY, "intro_text_arrived"),
    Phase.SHOW_LIST: Presentation(Expression.HAPPY, "show_list"),
    Phase.REQUEST: Presentation(Expression.WAITING, None),  # 항목별 발화, 런타임 결정
    Phase.WAIT_CHILD: Presentation(Expression.WAITING, None, silent=True),
    # JUDGE 는 무음이지만 thinking 을 '즉시' 표시해 판정 지연의 공백을 메운다 (§4.5).
    Phase.JUDGE: Presentation(Expression.THINKING, None, silent=True),
    Phase.CORRECT: Presentation(Expression.HAPPY, "correct_generic"),
    Phase.INCORRECT: Presentation(Expression.THINKING, "incorrect_generic"),
    Phase.DUPLICATE: Presentation(Expression.HAPPY, "duplicate_generic"),
    # 로봇 동작 중 발화 금지 — 아동 시선이 로봇 팔로 가야 한다 (§4.5).
    Phase.ROBOT_TURN: Presentation(Expression.NEUTRAL, None, silent=True),
    Phase.ROBOT_VERIFY: Presentation(Expression.THINKING, None, silent=True),
    # 로봇의 실패를 아동이 자기 실패로 받으면 안 된다 (§4.5 프롬프트 위계의 정신).
    Phase.ROBOT_FAIL: Presentation(Expression.THINKING, "robot_mistake"),
    Phase.NEXT: Presentation(Expression.NEUTRAL, None, silent=True),
    Phase.COMPLETE: Presentation(Expression.CELEBRATING, "complete"),
    Phase.END: Presentation(Expression.NEUTRAL, None, silent=True),
    Phase.PAUSED: Presentation(Expression.WAITING, "pause_break"),
}

#: 운영자가 `force_state` 로 강제 전이할 수 있는 상태. PAUSED 는 pause/resume 전용.
FORCEABLE: frozenset[Phase] = frozenset(set(Phase) - {Phase.PAUSED})

#: 이 상태들에서는 로봇 팔이 움직이고 있다. 홀드 컨트롤러가 꺼져 있는 유일한 구간.
ROBOT_ACTIVE: frozenset[Phase] = frozenset({Phase.ROBOT_TURN})


def is_terminal(phase: Phase) -> bool:
    return phase is Phase.END
