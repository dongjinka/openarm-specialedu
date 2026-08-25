"""지각 → 판정. 프레임 1장을 받아 §5 계약대로 답한다."""

from __future__ import annotations

import logging
import time

from vlm_service.backends.base import parse_perception, parse_verify
from vlm_service.contract import (
    Judgment,
    Perception,
    VerifyJudgment,
    decide,
    decide_verify,
)
from vlm_service.prompts import SYSTEM, VERIFY_SYSTEM, VERIFY_USER, user_prompt

logger = logging.getLogger(__name__)


class JudgeService:
    def __init__(self, backend, *, min_confidence: float = 0.70) -> None:
        self.backend = backend
        self.min_confidence = min_confidence
        self.calls = 0
        self.last_latency_s: float | None = None

    async def perceive(self, image: bytes, checklist: list[str],
                       packed: list[str]) -> Perception:
        started = time.monotonic()
        try:
            raw = await self.backend.infer(image, SYSTEM, user_prompt(checklist, packed))
        except Exception as exc:  # noqa: BLE001
            logger.exception("VLM 호출 실패")
            return Perception(object="none", confidence=0.0, reason=f"오류: {exc}"[:120])
        finally:
            self.last_latency_s = round(time.monotonic() - started, 3)
            self.calls += 1
        return parse_perception(raw)

    async def judge(self, image: bytes, checklist: list[str], packed: list[str]) -> Judgment:
        perception = await self.perceive(image, checklist, packed)
        return decide(perception, checklist, packed, min_confidence=self.min_confidence)

    async def verify(self, image: bytes) -> VerifyJudgment:
        """사후 확인 — 배치면과 가방 상태를 함께 본다 (§ROBOT_VERIFY).

        판정(`judge`)과 묻는 것이 다르므로 프롬프트도 다르다.
        """
        started = time.monotonic()
        try:
            raw = await self.backend.infer(image, VERIFY_SYSTEM, VERIFY_USER)
        except Exception as exc:  # noqa: BLE001
            logger.exception("VLM 사후확인 호출 실패")
            return VerifyJudgment(object_in_bag=False, bag_closed=False, confidence=0.0,
                                  reason=f"오류: {exc}"[:120], hold_for_operator=True)
        finally:
            self.last_latency_s = round(time.monotonic() - started, 3)
            self.calls += 1
        return decide_verify(parse_verify(raw), min_confidence=self.min_confidence)
