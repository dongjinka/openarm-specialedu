"""상태 머신이 반환하는 부작용 서술.

상태 머신은 I/O 를 하지 않는다. 무엇을 해야 하는지만 값으로 돌려주고,
실제 전송·기록·타이머는 main.py 가 한다. 이 분리 덕분에
  - 서버 없이 전이를 테스트할 수 있고
  - 로그를 그대로 리플레이할 수 있고
  - 2~4단계(태블릿·로깅·VLM)가 상태 머신 수정 없이 가산적으로 붙는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orchestrator.events import Role


@dataclass(frozen=True)
class Broadcast:
    """태블릿·운영자 콘솔로 나가는 상태 갱신."""

    payload: dict[str, Any]
    #: AUDIO 도 받는다. 지금은 태블릿이 소리를 내지만, 스피커를 로봇 쪽으로 옮기면
    #: 그 스포크가 같은 `state` 를 듣고 재생한다 — 계약을 바꾸지 않고 갈아끼울 수 있다.
    roles: tuple[Role, ...] = (Role.TABLET, Role.OPERATOR, Role.AUDIO)


@dataclass(frozen=True)
class SendRobot:
    """로봇 브릿지로만 나간다. VLM 은 이 Effect 를 만들 수 없다."""

    payload: dict[str, Any]


@dataclass(frozen=True)
class RequestJudge:
    """VLM 에게 판정을 요청한다. 4단계 전까지는 운영자 콘솔이 대신 응답한다."""

    checklist: list[str] = field(default_factory=list)
    packed: list[str] = field(default_factory=list)
    target: str | None = None


@dataclass(frozen=True)
class RequestVerify:
    """로봇 턴 후 사후 확인 요청 — 물체 삽입 + 가방 닫힘."""

    cmd_id: str
    target: str | None = None


@dataclass(frozen=True)
class SetWatch:
    """배치 구역 감시 모드. 상태 머신이 명령하고 capture 스포크가 따른다.

    off   — 감시하지 않는다.
    judge — 변화 → 정지 → VLM 1회 → child_placed. WAIT_CHILD 에서만.
    guard — **VLM 을 부르지 않는다.** 프레임 차분만 보고 변화가 생기면 운영자에게 알린다.
            ROBOT_TURN 중 책상에 물건이 올라오면 정책이 본 적 없는 관측이 되는데,
            데이터셋 60에피소드의 이송 구간에는 물건이 놓인 프레임이 하나도 없다.
    """

    mode: str


@dataclass(frozen=True)
class SetListen:
    """마이크 창. 상태 머신이 명령하고 voice 스포크가 따른다.

    omni STT 에는 VAD 가 없다 — "버튼을 뗀 시점이 곧 발화 끝"이다. 그래서 언제
    듣는지를 우리가 정해야 하고, 그 덕에 **에코 문제가 같이 풀린다**: 로봇이 말하는
    동안과 팔이 도는 동안(모터 소음 1~2kHz +13dB)에는 아예 열지 않는다.

    off  — 닫는다.  open — 아동이 말할 수 있는 구간이라 창을 반복해서 연다.
    """

    mode: str


@dataclass(frozen=True)
class Log:
    """§7 JSONL 한 줄."""

    event: str
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StartTimer:
    name: str
    ms: int
    cmd_id: str | None = None


@dataclass(frozen=True)
class CancelTimer:
    name: str


Effect = (Broadcast | SendRobot | RequestJudge | RequestVerify | SetWatch | SetListen
          | Log | StartTimer | CancelTimer)
