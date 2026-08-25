"""역할별 WebSocket 레지스트리.

배선이 곧 아키텍처다. `send_robot` 은 ROBOT 역할에만 보내고, VLM 소켓이 만들 수
있는 아웃바운드 경로는 존재하지 않는다 — §12 의 "VLM → VLA 직접 호출 금지"를
문서가 아니라 코드 구조로 강제한다.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

from fastapi import WebSocket

from orchestrator.events import Role

logger = logging.getLogger(__name__)


class Hub:
    def __init__(self) -> None:
        self._peers: dict[Role, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def join(self, role: Role, ws: WebSocket) -> None:
        async with self._lock:
            self._peers[role].add(ws)
        logger.info("연결 role=%s (총 %d)", role.value, len(self._peers[role]))

    async def leave(self, role: Role, ws: WebSocket) -> None:
        async with self._lock:
            self._peers[role].discard(ws)

    def count(self, role: Role) -> int:
        return len(self._peers[role])

    async def send(self, roles: tuple[Role, ...], payload: dict[str, Any]) -> None:
        targets: list[tuple[Role, WebSocket]] = []
        async with self._lock:
            for role in roles:
                targets.extend((role, ws) for ws in self._peers[role])
        dead: list[tuple[Role, WebSocket]] = []
        for role, ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001 — 끊긴 소켓은 조용히 정리한다
                dead.append((role, ws))
        for role, ws in dead:
            await self.leave(role, ws)

    async def send_robot(self, payload: dict[str, Any]) -> None:
        await self.send((Role.ROBOT,), payload)

    def health(self) -> dict[str, int]:
        return {role.value: len(peers) for role, peers in self._peers.items()}
