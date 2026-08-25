"""VLM 백엔드 인터페이스. 모델 교체가 서비스 코드를 건드리지 않게 한다."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Protocol

from vlm_service.contract import BagState, ObjectClass, Perception, VerifyPerception


class VlmBackend(Protocol):
    name: str

    async def infer(self, image_bytes: bytes, system: str, user: str) -> str:
        """모델의 raw 텍스트 응답."""
        ...


def encode(image: bytes | str | Path) -> tuple[bytes, str]:
    if isinstance(image, (str, Path)):
        data = Path(image).read_bytes()
    else:
        data = image
    mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return data, mime


def data_url(image: bytes) -> str:
    data, mime = encode(image)
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


_JSON = re.compile(r"\{.*\}", re.S)


def parse_perception(raw: str) -> Perception:
    """모델이 코드펜스나 군더더기를 붙여도 살려낸다.

    파싱 실패는 `none` + confidence 0 으로 떨어뜨린다 — 추측하지 않고 사람에게 넘긴다.
    """
    match = _JSON.search(raw or "")
    if not match:
        return Perception(object=ObjectClass.NONE, confidence=0.0, reason="파싱 실패")
    try:
        body = json.loads(match.group(0))
    except json.JSONDecodeError:
        return Perception(object=ObjectClass.NONE, confidence=0.0, reason="JSON 아님")

    value = str(body.get("object", "none")).strip().lower()
    if value not in {c.value for c in ObjectClass}:
        value = ObjectClass.OTHER.value if value else ObjectClass.NONE.value
    try:
        conf = max(0.0, min(1.0, float(body.get("confidence", 0.0))))
    except (TypeError, ValueError):
        conf = 0.0
    return Perception(object=ObjectClass(value), confidence=conf,
                      reason=str(body.get("reason", ""))[:120])


def parse_verify(raw: str) -> VerifyPerception:
    """사후 확인 응답 파싱.

    실패는 `bag=hidden` 으로 떨어뜨린다 — `closed` 로 추측하면 열린 가방으로 다음 턴이
    시작되고, `open` 으로 추측하면 성공한 턴이 아동에게 '로봇의 실수'로 안내된다.
    `hidden` 은 사람에게 넘어가므로 둘 다 피한다.
    """
    fail = VerifyPerception(surface=ObjectClass.NONE, bag=BagState.HIDDEN,
                            confidence=0.0, reason="파싱 실패")
    match = _JSON.search(raw or "")
    if not match:
        return fail
    try:
        body = json.loads(match.group(0))
    except json.JSONDecodeError:
        return fail

    surface = str(body.get("surface", "none")).strip().lower()
    if surface not in {c.value for c in ObjectClass}:
        surface = ObjectClass.OTHER.value if surface else ObjectClass.NONE.value
    bag = str(body.get("bag", "hidden")).strip().lower()
    if bag not in {b.value for b in BagState}:
        bag = BagState.HIDDEN.value
    try:
        conf = max(0.0, min(1.0, float(body.get("confidence", 0.0))))
    except (TypeError, ValueError):
        conf = 0.0
    return VerifyPerception(surface=ObjectClass(surface), bag=BagState(bag),
                            confidence=conf, reason=str(body.get("reason", ""))[:120])
