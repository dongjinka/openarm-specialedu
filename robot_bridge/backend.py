"""로봇 백엔드 인터페이스.

`--sim` 과 실제 GR00T 추론이 이 한 인터페이스를 공유한다. 오케스트레이터는 어느
쪽이 붙어 있는지 알지 못한다 — 계약이 정책 교체와 무관하게 유지되는 이유다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol


@dataclass(frozen=True)
class RobotOutcome:
    success: bool
    reason: str  # DoneReason 값: verified | timeout | operator | aborted
    duration_ms: int
    detail: str = ""


ProgressCb = Callable[[int], Awaitable[None]]


class RobotBackend(Protocol):
    name: str

    def ready(self) -> bool: ...

    def read_state(self) -> list[float]:
        """현재 16차원 관절 상태. preflight 검사의 입력."""
        ...

    async def execute(self, cmd_id: str, target: str | None, deadline_ms: int,
                      on_progress: ProgressCb) -> RobotOutcome:
        """가방 열기 → 물건 1개 넣기 → 가방 닫기 한 사이클을 끝까지 수행한다."""
        ...

    async def abort(self, cmd_id: str, reason: str) -> None:
        """즉시 정지. 백드라이버블이라 하드 스톱이 아니라 홀드로 램프다운해야 한다."""
        ...
