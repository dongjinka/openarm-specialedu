"""`--sim` 백엔드 — 하드웨어 없이 계약만 대역한다.

UI·오케스트레이터를 로봇 없이 병렬 개발하기 위한 것이다 (§6).

기본 지연은 3초다. 개발이 30초짜리 대기에 막히면 안 되기 때문이다.
`--realistic` 을 주면 실측값(평균 46.3초)으로 돌려 리허설에서 진짜 침묵 구간의
길이를 체감할 수 있게 한다. 3항목이면 2분 18초가 무발화다 — 이 감각이 §C-3 의
촉진 타이머와 단계 표시가 필요한 이유다.
"""

from __future__ import annotations

import asyncio
import io
import itertools
import random
from pathlib import Path

from robot_bridge.backend import ProgressCb, RobotOutcome
from robot_bridge.preflight import envelope_midpoint

#: 데이터셋 실측 — 83,351 프레임 / 60 에피소드 / 30fps = 평균 46.3초.
#: 에피소드 길이 분포는 중앙 45.9초 · 최소 40.7초 · 최대 60.5초였다.
#: 주의: 이건 **데이터셋 시간**이다. 실제 벽시계 실행 시간은 달성 루프 레이트에
#: 따라 더 길 수 있으므로, 드라이런으로 재측정하면 이 값도 같이 고친다.
REALISTIC_MEAN_MS = 46_300
REALISTIC_JITTER_MS = 5_000
DEFAULT_MS = 3_000
PROGRESS_INTERVAL_MS = 2_000


class SimBackend:
    name = "sim"

    def __init__(self, *, frames_dir: str | None = None,
                 duration_ms: int = DEFAULT_MS, realistic: bool = False,
                 fail_rate: float = 0.0, seed: int | None = None) -> None:
        self.realistic = realistic
        # 로봇 없이도 /frame/latest 를 띄워 감시·판정 전 구간을 돌려보기 위한 것.
        self._frames = sorted(Path(frames_dir).glob("*.jpg")) if frames_dir else []
        self._cycle = itertools.cycle(self._frames) if self._frames else None
        self.duration_ms = REALISTIC_MEAN_MS if realistic else duration_ms
        self.fail_rate = fail_rate
        self._rng = random.Random(seed)
        self._aborted: set[str] = set()
        self._state = envelope_midpoint()

    def ready(self) -> bool:
        return True

    def read_state(self) -> list[float]:
        return list(self._state)

    async def abort(self, cmd_id: str, reason: str) -> None:
        self._aborted.add(cmd_id)

    async def execute(self, cmd_id: str, target: str | None, deadline_ms: int,
                      on_progress: ProgressCb) -> RobotOutcome:
        planned = self.duration_ms
        if self.realistic:
            planned = max(1_000, int(self._rng.gauss(REALISTIC_MEAN_MS, REALISTIC_JITTER_MS)))

        elapsed = 0
        step = min(PROGRESS_INTERVAL_MS, max(200, planned // 4))
        while elapsed < planned:
            await asyncio.sleep(step / 1000)
            elapsed += step
            if cmd_id in self._aborted:
                self._aborted.discard(cmd_id)
                return RobotOutcome(False, "aborted", elapsed, "운영자/오케스트레이터 중단")
            if elapsed >= deadline_ms:
                return RobotOutcome(False, "timeout", elapsed, "데드라인 초과")
            await on_progress(elapsed)

        success = self._rng.random() >= self.fail_rate
        return RobotOutcome(success, "verified" if success else "aborted", elapsed,
                            "" if success else "시뮬레이션 실패 주입")


    def latest_frame(self) -> bytes | None:
        """디렉터리 프레임을 순서대로. 없으면 단색 프레임 — 감시는 변화가 없다고 본다."""
        if self._cycle is not None:
            return next(self._cycle).read_bytes()
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (640, 480), (200, 200, 200)).save(buf, "JPEG", quality=85)
        return buf.getvalue()
