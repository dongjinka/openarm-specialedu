#!/usr/bin/env python3
"""운영자 콘솔 CLI — 2단계의 브라우저 콘솔이 나오기 전까지 쓰는 WoZ 폴백.

VLM 이 아직 없으므로 판정도 사람이 한다 (§5 의 운영자 폴백 경로).
운영자 수동 판정은 데모까지 유지된다 (§12).

    python tools/operator_cli.py                 # 대화형
    python tools/operator_cli.py --auto          # 전부 정답으로 자동 응답 (스모크)

대화형 명령:
    s        세션 시작          o        정답 (호명된 물건을 놓았다)
    x        오답              d        중복
    v        확인 성공         vf       확인 실패 (물건이 안 들어감)
    p / r    일시정지 / 재개    a        로봇 중단
    n        다음으로 — 태블릿이 끊겼을 때 진행을 손으로 민다
    q        종료
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys

import websockets

BASE = "ws://127.0.0.1:8000"


class Console:
    def __init__(self, base: str, auto: bool) -> None:
        self.base = base
        self.auto = auto
        self.target: str | None = None
        self.cmd_id: str | None = None
        self.phase: str = "?"
        self.done = asyncio.Event()

    async def run(self) -> None:
        async with (
            websockets.connect(f"{self.base}/ws/operator") as op,
            websockets.connect(f"{self.base}/ws/vlm") as vlm,
        ):
            self.op, self.vlm = op, vlm
            tasks = [asyncio.create_task(self._listen(op, "op")),
                     asyncio.create_task(self._listen(vlm, "vlm"))]
            if not self.auto:
                tasks.append(asyncio.create_task(self._stdin()))
            else:
                await asyncio.sleep(0.3)
                await self._send(op, {"type": "session_start", "scenario_id": "minsu_playdate_v1"})
            try:
                await self.done.wait()
            finally:
                for t in tasks:
                    t.cancel()

    async def _send(self, ws, payload: dict) -> None:
        await ws.send(json.dumps(payload, ensure_ascii=False))

    async def _listen(self, ws, who: str) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "state":
                self.phase = msg["phase"]
                self.target = msg.get("target")
                prog = msg.get("progress", {})
                line = (f"[{self.phase:<13}] 대상={self.target or '-':<7} "
                        f"담김={prog.get('packed', [])} 남음={prog.get('remaining', [])}")
                if msg.get("utterance_id"):
                    line += f"  🔊 {msg['utterance_id']}"
                print(line, flush=True)
                if self.phase == "END":
                    self.done.set()
                if self.auto and self.phase == "WAIT_CHILD":
                    await self._send(self.op, {"type": "child_placed"})
            elif kind == "judge_request":
                self.target = msg.get("target")
                if self.auto and who == "vlm" and self.target:
                    await self._send(self.vlm, {"type": "judge", "object": self.target,
                                                "should_pack": True, "confidence": 0.95})
                elif who == "op":
                    print(f"   ▸ 판정 대기 — 호명: {self.target}   (o=정답 x=오답 d=중복)", flush=True)
            elif kind == "verify_request":
                self.cmd_id = msg.get("cmd_id")
                if self.auto and who == "op":
                    await self._send(self.op, {"type": "verify_result", "cmd_id": self.cmd_id,
                                               "object_in_bag": True, "bag_closed": True})
                elif who == "op":
                    print(f"   ▸ 사후 확인 — cmd={self.cmd_id}  (v=성공 vf=실패)", flush=True)
            elif kind == "progress_tick":
                print(f"   … 로봇 동작 중 {msg['elapsed_ms'] / 1000:.1f}s", flush=True)
            elif kind == "operator_attention":
                print(f"   ⚠ 운영자 확인 필요: {msg.get('reason')}", flush=True)
            elif kind == "error":
                print(f"   ✗ {msg.get('detail')}", flush=True)

    async def _stdin(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            cmd = (await loop.run_in_executor(None, sys.stdin.readline)).strip()
            if not cmd:
                continue
            if cmd == "q":
                self.done.set()
                return
            await self._dispatch(cmd)

    async def _dispatch(self, cmd: str) -> None:
        op, vlm, tgt = self.op, self.vlm, self.target
        match cmd:
            case "s":
                await self._send(op, {"type": "session_start", "scenario_id": "minsu_playdate_v1"})
            case "c":
                await self._send(op, {"type": "child_placed"})
            case "o" if tgt:
                await self._send(vlm, {"type": "judge", "object": tgt,
                                       "should_pack": True, "confidence": 0.95})
            case "x":
                await self._send(op, {"type": "judge_override", "object": "car",
                                      "should_pack": False})
            case "d":
                await self._send(op, {"type": "judge_override", "object": tgt or "flower",
                                      "should_pack": True})
            case "v":
                await self._send(op, {"type": "verify_result", "cmd_id": self.cmd_id or "",
                                      "object_in_bag": True, "bag_closed": True})
            case "vf":
                await self._send(op, {"type": "verify_result", "cmd_id": self.cmd_id or "",
                                      "object_in_bag": False, "bag_closed": True})
            case "p":
                await self._send(op, {"type": "pause"})
            case "r":
                await self._send(op, {"type": "resume"})
            case "a":
                await self._send(op, {"type": "robot_abort", "cmd_id": self.cmd_id})
            case "n":
                # 발화가 끝났다는 신호를 사람이 대신 낸다.
                # 진행은 보통 태블릿이 재생을 마치며 보내는 advance 로 일어난다.
                # 태블릿이 끊기거나 소리가 막히면 그 신호가 오지 않아 세션이
                # 그 자리에 선다 — 손으로 밀 수단이 없으면 시연이 멈춘다.
                await self._send(op, {"type": "advance"})
            case _:
                print(f"   ? 알 수 없는 명령: {cmd}", flush=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default=BASE)
    p.add_argument("--auto", action="store_true", help="전부 정답으로 자동 응답")
    args = p.parse_args()
    if not args.auto:
        print(__doc__)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(Console(args.base, args.auto).run())


if __name__ == "__main__":
    main()
