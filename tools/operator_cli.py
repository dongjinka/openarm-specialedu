#!/usr/bin/env python3
"""운영자 콘솔 — 시연 중 사람이 개입하는 유일한 통로.

    python tools/operator_cli.py

평소에는 **s 하나만 누른다.** 나머지는 자동 경로(VLM 판정·감시, 태블릿 재생)가
멈췄을 때 손으로 미는 수단이고, 그 상황은 화면에 `⚠ 운영자 확인 필요` 로 뜬다.

    ── 시연 중 ─────────────────────────────────────────────
    s     세션 시작            (이것만 사람이 반드시 누른다)
    p / r 일시정지 / 재개      (아이가 쉬어야 할 때)
    a     로봇 중단            (동작이 이상하면 — 물리 비상정지가 우선이다)
    q     콘솔 종료            (세션과 로봇은 계속 돈다)

    ── 자동 경로가 멈췄을 때만 ──────────────────────────────
    o / x / d   판정: 정답 / 오답 / 이미 담은 것
    v / vf      사후 확인: 들어갔다 / 안 들어갔다
    c           아이가 물건을 놓았다
    n           발화가 끝났다 — 태블릿이 끊기거나 소리가 막혀 멈췄을 때

`q` 는 콘솔만 닫는다. 로봇을 내리려면 런처 터미널에서 Ctrl-C 를 눌러야 한다 —
그래야 시작 자세 복귀 → 하강 → 토크 해제가 돈다.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys

import websockets

BASE = "ws://127.0.0.1:8000"

#: 체크리스트에 없는 물건. `x`(오답) 이 쓴다.
#:
#: 예전에는 `"car"` 라는 없는 이름을 지어내 보냈는데, 그 값이 `child_response` 로그의
#: `object` 에 그대로 남아 평가 데이터를 더럽혔다. `other` 는 VLM 이 실제로 내는
#: 클래스(`vlm_service/contract.py:ObjectClass`)라 로그가 일관된다.
NOT_ON_LIST = "other"


class Console:
    """오케스트레이터에 `operator` 역할로 붙는다. 소켓은 이것 하나뿐이다.

    예전에는 `/ws/vlm` 에도 같이 붙어 `o`(정답) 를 VLM 인 척 보냈다. 두 가지가
    나빴다 — 진짜 `vlm_service` 와 같은 역할 슬롯을 나눠 갖게 되고(`set_watch` 가
    양쪽으로 간다), 정답만 `judge` 이고 오답·중복은 `judge_override` 라 경로가
    갈렸다. 셋 다 `judge_override` 로 보내면 소켓 하나로 끝난다.

    `judge_request`·`verify_request` 는 오케스트레이터가 VLM 과 OPERATOR **양쪽에**
    보내므로(`orchestrator/main.py`), 화면 표시에도 vlm 소켓이 필요 없다.
    """

    def __init__(self, base: str) -> None:
        self.base = base
        self.target: str | None = None
        self.packed: list[str] = []
        self.cmd_id: str | None = None
        self.phase: str = "?"
        self.done = asyncio.Event()

    async def run(self) -> None:
        async with websockets.connect(f"{self.base}/ws/operator") as op:
            self.op = op
            tasks = [asyncio.create_task(self._listen(op)),
                     asyncio.create_task(self._stdin())]
            try:
                await self.done.wait()
            finally:
                for t in tasks:
                    t.cancel()

    async def _send(self, payload: dict) -> None:
        await self.op.send(json.dumps(payload, ensure_ascii=False))

    async def _listen(self, ws) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "state":
                self.phase = msg["phase"]
                self.target = msg.get("target")
                prog = msg.get("progress", {})
                self.packed = list(prog.get("packed", []))
                line = (f"[{self.phase:<13}] 대상={self.target or '-':<7} "
                        f"담김={self.packed} 남음={prog.get('remaining', [])}")
                if msg.get("utterance_id"):
                    line += f"  🔊 {msg['utterance_id']}"
                print(line, flush=True)
                if self.phase == "END":
                    self.done.set()
            elif kind == "judge_request":
                self.target = msg.get("target")
                print(f"   ▸ 판정 대기 — 호명: {self.target}   (o=정답 x=오답 d=이미담음)",
                      flush=True)
            elif kind == "verify_request":
                self.cmd_id = msg.get("cmd_id")
                print(f"   ▸ 사후 확인 — cmd={self.cmd_id}  (v=들어갔다 vf=안들어갔다)",
                      flush=True)
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
        match cmd:
            # ── 시연 중 ────────────────────────────────────────────────
            case "s":
                await self._send({"type": "session_start",
                                  "scenario_id": "minsu_playdate_v1"})
            case "p":
                await self._send({"type": "pause"})
            case "r":
                await self._send({"type": "resume"})
            case "a":
                await self._send({"type": "robot_abort", "cmd_id": self.cmd_id})

            # ── 폴백: 판정 (VLM 이 vlm_hold / judge_timeout 을 냈을 때) ──
            case "o":
                if not self.target:
                    print("   ? 호명된 물건이 없다 — 판정 대기 상태에서만 쓴다", flush=True)
                    return
                await self._send({"type": "judge_override",
                                  "object": self.target, "should_pack": True})
            case "x":
                await self._send({"type": "judge_override",
                                  "object": NOT_ON_LIST, "should_pack": False})
            case "d":
                # 중복은 **이미 담은 것**을 다시 올렸다는 뜻이다. 호명된 물건은
                # 정의상 아직 안 담겼으므로 그것을 보내면 중복으로 판정되지 않는다.
                if not self.packed:
                    print("   ? 아직 담은 것이 없다 — 중복일 수 없다", flush=True)
                    return
                await self._send({"type": "judge_override",
                                  "object": self.packed[-1], "should_pack": True})

            # ── 폴백: 사후 확인 (verify_hold) ──────────────────────────
            case "v":
                await self._send({"type": "verify_result", "cmd_id": self.cmd_id or "",
                                  "object_in_bag": True, "bag_closed": True})
            case "vf":
                await self._send({"type": "verify_result", "cmd_id": self.cmd_id or "",
                                  "object_in_bag": False, "bag_closed": True})

            # ── 폴백: 자동 감지가 놓쳤을 때 ────────────────────────────
            case "c":
                # 보통은 VLM 감시(watcher)가 변화→정지를 보고 스스로 낸다.
                # `--no-vlm` 으로 돌리거나 감시가 놓쳤을 때의 유일한 트리거다.
                await self._send({"type": "child_placed"})
            case "n":
                # 발화가 끝났다는 신호를 사람이 대신 낸다. 진행은 보통 태블릿이
                # 재생을 마치며 보내는 advance 로 일어나고, 태블릿이 붙어 있으면
                # 오케스트레이터의 자동 전진은 물러난다. 그래서 태블릿이 끊기거나
                # 소리가 막히면 세션이 그 자리에 서고, 손으로 밀 수단이 이것뿐이다.
                await self._send({"type": "advance"})

            case _:
                print(f"   ? 알 수 없는 명령: {cmd}", flush=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default=BASE)
    args = p.parse_args()
    print(__doc__)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(Console(args.base).run())


if __name__ == "__main__":
    main()
