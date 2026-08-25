"""배치 구역 감시 — 싼 프레임 차분이 비싼 VLM 호출을 게이팅한다.

**"비었는지 확인"과 "무엇인지 판정"을 나누지 않는다.** 안정된 변화마다 VLM 을 딱 1회
부르고, 답이 `none` 이면 아이가 물건을 치운 것이고 물체면 판정이다. 이 하나로
"오답 물건이 책상에 남아 같은 판정을 무한 반복"하는 문제가 사라진다 — 변화가 없으면
호출도 없고, 아이가 치우면 `none` 이 나와 다시 무장한다.

모드는 상태 머신이 명령한다 (`set_watch`).

    off    감시하지 않는다.
    judge  변화 → 정지 → VLM 1회 → 물체면 child_placed.  (WAIT_CHILD)
    guard  같은 정지 판정을 쓰되 **VLM 을 부르지 않고** zone_disturbed 만 낸다.
           (ROBOT_TURN)

`guard` 의 한계를 먼저 밝혀둔다. 데이터셋 ep0 으로 재생해 본 결과 프레임 차분으로는
**"팔이 움직였다"와 "물건이 나타났다"를 가를 수 없다** — 이송 중 팔이 수 초씩 멈추기
때문이다. 정지 요구를 1초→4초로 늘려도 턴당 4건, 배치 구역만 잘라 보면 오히려 6건이
나왔다. 그래서 판정을 시도하지 않고 **턴당 한 번만** 운영자를 부른다. 운영자는 같은
방에서 책상을 직접 보고 있으므로, 이 신호는 판단이 아니라 눈길을 돌리게 하는 용도다.

`guard` 가 필요한 이유: 씬 카메라는 GR00T 의 필수 입력 3개 중 하나인데,
데이터셋 60에피소드의 이송 구간에는 **책상에 물건이 놓인 프레임이 하나도 없다.**
이송 중 새 물건이 올라오면 정책이 본 적 없는 관측이 된다.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

#: 폴링 주기. 카메라는 30fps 지만 사람이 물건을 놓는 동작에 그렇게 빠를 필요는 없다.
#: 0.33초(3Hz)에서 0.6초(≈1.7Hz)로 넓혔다 — API 비용 절감 목적. 폴링 자체는 무료지만
#: (프레임 차분은 로컬), 주기가 넓으면 안정 판정(STABLE_POLLS)까지 걸리는 시간도 늘어나
#: 손이 스치는 순간적 흔들림이 매번 새 판정 사이클을 여는 것을 줄여 준다. 안정 판정까지는
#: 여전히 STABLE_POLLS × POLL_INTERVAL_S ≈ 1.8초로, 아이가 기다리기엔 충분히 짧다.
POLL_INTERVAL_S = 0.6
#: 차분 임계값 (0~255 그레이스케일 평균 절대차). 이보다 크면 '움직임'으로 본다.
#:
#: 데이터셋 씬 카메라 ep0 을 3Hz 로 훑어 정한 값이다 (60.4초, 정지 34쌍 · 움직임 36쌍):
#:     진짜 정지  중앙 0.47 · 최대 2.42
#:     실제 움직임 중앙 7.14
#:     임계값 2.5 → 정지를 움직임으로 오인 0/34 · 움직임 폴 놓침 5/36
#:
#: 두 오류는 대칭이 아니다. **정지를 움직임으로 보면 영영 정지 판정이 나지 않아**
#: 감시가 죽는다. 반대로 움직임 폴 몇 개를 놓치는 건 무해하다 — 움직임은 여러 폴에
#: 걸쳐 있고, 발화는 STABLE_POLLS 연속 정지를 요구하기 때문이다. 그래서 오인 0 쪽으로 잡았다.
MOTION_THRESHOLD = 2.5
#: 이만큼 연속으로 잠잠해야 '정지'로 친다. 3 × 0.33초 ≈ 1초 — 손이 빠져나갈 시간이다.
STABLE_POLLS = 3
#: 비교 해상도. 작을수록 조명 흔들림에 둔감하다.
DIFF_SIZE = (64, 48)
#: guard 모드에서 정지로 볼 폴 수. 이송 중 팔은 자주 멈추므로 판정보다 길게 잡는다.
GUARD_STABLE_POLLS = 6


def _fingerprint(data: bytes) -> list[int]:
    """프레임을 작은 그레이스케일 격자로 줄인다. 차분 비교용."""
    from PIL import Image

    img = Image.open(io.BytesIO(data)).convert("L").resize(DIFF_SIZE, Image.BILINEAR)
    return list(img.tobytes())


def _distance(a: list[int], b: list[int]) -> float:
    if not a or not b or len(a) != len(b):
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


@dataclass
class CachedVerdict:
    """안정 프레임에서 낸 판정. 뒤이어 오는 judge_request 를 이걸로 답한다.

    캐시가 없으면 watcher 가 한 번, orchestrator 의 judge_request 가 또 한 번
    프레임을 찍어 **2회 호출 + 레이스**가 된다.
    """

    payload: dict
    fingerprint: list[int]
    at: float

    def fresh(self, now: float, ttl_s: float) -> bool:
        return (now - self.at) <= ttl_s


class ZoneWatcher:
    """모드에 따라 도는 단일 루프. 상태 머신이 모드를 소유한다."""

    def __init__(
        self,
        frames,
        judge: Callable[[bytes], Awaitable[dict | None]],
        emit: Callable[[dict], Awaitable[None]],
        *,
        cache_ttl_s: float = 6.0,
    ) -> None:
        self.frames = frames
        self.judge = judge          # 프레임 → judge payload(dict) 또는 None(=비었음)
        self.emit = emit            # 오케스트레이터로 보내기
        self.cache_ttl_s = cache_ttl_s
        self.mode = "off"
        self.cache: CachedVerdict | None = None
        self._task: asyncio.Task | None = None
        self._baseline: list[int] | None = None
        self._last: list[int] | None = None
        self._still = 0
        self._moved = False
        self._alerted = False

    # ── 모드 전환 ────────────────────────────────────────────────────────
    def set_mode(self, mode: str) -> None:
        if mode == self.mode:
            return
        logger.info("감시 모드 %s → %s", self.mode, mode)
        self.mode = mode
        # `_baseline`(무장 기준)만 지운다. `_last`(직전 프레임)를 함께 지우면
        # `cached()` 의 "그 사이 장면이 또 바뀌었나" 검사가 **항상 통과**해 버린다 —
        # 판정 직후 모드가 off 로 내려가므로, 그 검사가 실제로는 한 번도 안 돈다.
        self._baseline = None
        self._still = 0
        self._moved = False
        self._alerted = False
        if mode == "off":
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    def _stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def aclose(self) -> None:
        self._stop()

    # ── 루프 ─────────────────────────────────────────────────────────────
    async def _loop(self) -> None:
        try:
            # 무장 직후 1회는 조건 없이 본다 — 로봇 턴 동안 미리 올려둔 물건을 회수한다.
            if self.mode == "judge":
                await self._evaluate(first=True)
            while True:
                await asyncio.sleep(POLL_INTERVAL_S)
                await self._tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("감시 루프 실패 — 운영자 버튼 폴백으로 진행한다")

    async def _tick(self) -> None:
        try:
            data = await self.frames.latest()
        except Exception as exc:  # noqa: BLE001
            logger.warning("프레임 획득 실패: %s", exc)
            return
        current = _fingerprint(data)

        if self._baseline is None:
            self._baseline, self._last = current, current
            return

        motion = _distance(self._last, current)
        self._last = current

        if motion > MOTION_THRESHOLD:
            self._moved = True          # 무언가 움직이는 중 — 손이든 팔이든
            self._still = 0
            return

        if not self._moved:
            return                      # 처음부터 조용했다. 볼 것 없음

        self._still += 1
        needed = GUARD_STABLE_POLLS if self.mode == "guard" else STABLE_POLLS
        if self._still < needed:
            return

        # 움직임이 멎었다. 무장 시점과 견줘 실제로 달라진 게 있는지 본다.
        changed = _distance(self._baseline, current) > MOTION_THRESHOLD
        self._moved = False
        self._still = 0
        if not changed:
            return                      # 흔들렸다 제자리로. 호출하지 않는다

        self._baseline = current
        if self.mode == "guard":
            await self._alert()
        else:
            await self._evaluate(data=data, fingerprint=current)

    # ── 모드별 동작 ──────────────────────────────────────────────────────
    async def _alert(self) -> None:
        if self._alerted:
            return          # 턴당 한 번이면 족하다. 반복하면 운영자가 무시하게 된다.
        self._alerted = True
        logger.warning("로봇 동작 중 배치 구역에 변화 — 운영자 호출")
        await self.emit({"type": "zone_disturbed",
                         "detail": "로봇 동작 중 배치 구역이 바뀌었다"})

    async def _evaluate(self, *, data: bytes | None = None,
                        fingerprint: list[int] | None = None, first: bool = False) -> None:
        if data is None:
            try:
                data = await self.frames.latest()
            except Exception as exc:  # noqa: BLE001
                logger.warning("프레임 획득 실패: %s", exc)
                return
            fingerprint = _fingerprint(data)
            self._baseline = self._last = fingerprint

        payload = await self.judge(data)
        if payload is None:
            # 배치면이 비었다. 아이가 물건을 치웠거나 아직 놓지 않았다.
            # child_placed 를 내지 않고 다시 무장한다 — 이게 오답 반복을 끊는 지점이다.
            if not first:
                logger.info("배치면이 비었다 — 다시 무장")
            self.cache = None
            return

        self.cache = CachedVerdict(payload, fingerprint or [], time.monotonic())
        await self.emit({"type": "child_placed"})

    # ── 캐시 조회 ────────────────────────────────────────────────────────
    def cached(self) -> dict | None:
        """직전 안정 프레임의 판정. 신선하고 장면이 그대로면 재호출하지 않는다."""
        if self.cache is None or not self.cache.fresh(time.monotonic(), self.cache_ttl_s):
            return None
        if self._last and _distance(self.cache.fingerprint, self._last) > MOTION_THRESHOLD:
            return None                 # 그 사이 장면이 또 바뀌었다. 다시 본다
        return self.cache.payload
