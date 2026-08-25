#!/usr/bin/env python3
"""Ran 과제분석(2026-08-21) 대로 세션을 한 번 돌리고, 로그가 그 지표를 내는지 본다.

두 가지를 확인한다.

  1. **흐름** — 문서 ①~⑥ 이 실제 상태 전이로 일어나는가.
  2. **지표** — 9개 하위과제의 '권장 행동지표' 를 로그에서 뽑을 수 있는가.

로봇은 `--sim`, VLM·음성은 대역이다. 검사 대상은 모델 성능이 아니라 **계약과 로그**다.

    .venv/bin/python tools/scenario_check.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import sys
from dataclasses import replace
from pathlib import Path

import uvicorn
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.main import create_app                      # noqa: E402
from orchestrator.scenario import find_scenario               # noqa: E402
from robot_bridge.main import BridgeClient                    # noqa: E402
from robot_bridge.sim import SimBackend                       # noqa: E402
from voice_service.intents import classify                    # noqa: E402

#: 문서 ①~⑥ 을 상태 전이로 옮긴 것. 순서대로 나타나야 한다.
SCRIPT = [
    ("①", "INTRO", "친구의 문자와 장난감 목록 제시"),
    ("①", "SHOW_LIST", "목록 확인"),
    ("③", "REQUEST", "목록의 한 항목을 호명"),
    ("③", "WAIT_CHILD", "아동이 주변에서 찾아 책상에 올림"),
    ("④", "INCORRECT", "오답 → '그건 아닌 것 같아' 만"),
    ("④", "CORRECT", "정답 → 칭찬"),
    ("④", "ROBOT_TURN", "가방에 넣고 닫음"),
    ("⑤", "NEXT", "물건 개수만큼 반복"),
    ("⑥", "COMPLETE", "'이제 갈 준비 완료!' 인사"),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def send(ws, payload: dict) -> None:
    await ws.send(json.dumps(payload, ensure_ascii=False))


async def run_session(log_dir: Path) -> tuple[list[dict], list[str]]:
    base = find_scenario("minsu_playdate_v1")
    # 타이머만 줄인다. 계약은 그대로다.
    sc = replace(base, stall_timeout_ms=400, robot_deadline_ms=8_000, judge_timeout_ms=8_000)

    port = free_port()
    app = create_app(sc, log_dir=str(log_dir), advance_ms=40)
    orch = app.state.orchestrator
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    serving = asyncio.create_task(server.serve())
    for _ in range(300):
        if getattr(server, "started", False):
            break
        await asyncio.sleep(0.02)

    url = lambda role: f"ws://127.0.0.1:{port}/ws/{role}"     # noqa: E731
    phases: list[str] = []
    tasks: list[asyncio.Task] = []

    async with (
        websockets.connect(url("operator")) as op,
        websockets.connect(url("tablet")) as tab,
        websockets.connect(url("capture")) as cap,
        websockets.connect(url("vlm")) as vlm,
        websockets.connect(url("voice")) as voice,
    ):
        done = asyncio.Event()
        SPEAKS = {"INTRO", "SHOW_LIST", "REQUEST", "CORRECT", "INCORRECT",
                  "DUPLICATE", "ROBOT_FAIL", "COMPLETE"}
        # 항목별 아동 행동 대본. 문서 ④(오답 후 교체)와 하위과제 3·2 를 모두 태운다.
        plan = {"flower": ["wrong", "right"],     # 오답 → 자가 정정
                "whale":  ["stall", "right"],      # 가만히 있음 → 촉진
                "tree":   ["ask", "right"]}        # "뭐라고?" → 재호명
        step: dict[str, int] = {}

        async def tablet_loop():
            async for raw in tab:
                m = json.loads(raw)
                if m.get("type") != "state":
                    continue
                ph = m["phase"]
                if not phases or phases[-1] != ph:
                    phases.append(ph)
                if ph == "END":
                    done.set()
                elif ph in SPEAKS:
                    await send(tab, {"type": "advance", "from_phase": ph})

        async def child_loop():
            """WAIT_CHILD 에 들어올 때마다, 그리고 촉진을 들을 때마다 대본대로 행동한다.

            정체(`stall`)는 **한 번 흘려보내는 것**이지 영원한 무응답이 아니다.
            촉진 발화를 들으면 다음 행동으로 넘어간다.
            """
            async for raw in cap:
                m = json.loads(raw)
                if not (m.get("type") == "set_watch" and m.get("mode") == "judge"):
                    continue
                target = orch.state.target(sc)
                if target is None:
                    continue
                i = step.get(target, 0)
                step[target] = i + 1
                action = plan[target][i] if i < len(plan[target]) else "right"
                await asyncio.sleep(0.15)          # 아동의 반응 시간
                if action == "stall":
                    # 정체 타이머가 한 번 돌 시간만 기다렸다가 행동한다.
                    await asyncio.sleep(sc.stall_timeout_ms / 1000 + 0.3)
                    await send(cap, {"type": "child_placed"})
                    return_to = plan[target]
                    if i + 1 < len(return_to):
                        step[target] = i + 1        # 다음 호출은 'right' 로
                    continue
                if action == "ask":
                    d = classify("뭐라고?")
                    await send(voice, {"type": "child_utterance", "text": "뭐라고?",
                                       "intent": d.intent.value, "confidence": d.confidence})
                    await asyncio.sleep(0.25)       # 다시 듣고 나서 올린다
                    await send(cap, {"type": "child_placed"})
                    continue
                await send(cap, {"type": "child_placed"})

        async def vlm_loop():
            async for raw in vlm:
                m = json.loads(raw)
                if m.get("type") == "judge_request":
                    target = m.get("target")
                    i = max(0, step.get(target, 1) - 1)
                    seq_ = plan.get(target, [])
                    wrong = i < len(seq_) and seq_[i] == "wrong"
                    seen = "car" if wrong else target
                    await send(vlm, {"type": "judge", "object": seen,
                                     "should_pack": not wrong, "confidence": 0.95})
                elif m.get("type") == "verify_request":
                    await send(vlm, {"type": "verify_result", "cmd_id": m["cmd_id"],
                                     "object_in_bag": True, "bag_closed": True})

        tasks = [asyncio.create_task(c) for c in (tablet_loop(), child_loop(), vlm_loop())]
        robot = asyncio.create_task(
            BridgeClient(url("robot"), SimBackend(duration_ms=200, seed=3)).run())
        tasks.append(robot)

        await asyncio.sleep(0.3)
        await send(op, {"type": "session_start", "scenario_id": sc.scenario_id})
        try:
            await asyncio.wait_for(done.wait(), timeout=60)
        except (TimeoutError, asyncio.TimeoutError):
            print("⚠️ 세션이 END 에 도달하지 못했다 — 지금까지의 로그로 보고한다\n")

    for t in tasks:
        t.cancel()
    server.should_exit = True
    with contextlib.suppress(Exception):
        await serving

    log_path = Path(orch.log.path)
    rows = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return rows, phases


# ── Ran 문서 4절: 하위과제별 '권장 행동지표' 를 로그에서 뽑는다 ──────────────
def indicators(rows: list[dict]) -> list[tuple[str, str, str, object]]:
    """(하위과제, 지표, 상태, 값). 상태는 ✅ 산출됨 / ⚠️ 부분 / ❌ 못 냄."""
    ev = lambda name: [r for r in rows if r["event"] == name]   # noqa: E731
    responses = ev("child_response")
    prompts = ev("prompt_given")
    utterances = ev("child_utterance")
    verifies = ev("robot_verify")
    states = ev("state_change")
    out: list[tuple[str, str, str, object]] = []

    # 1. 맥락 파악 — 시작까지 시간 · 재확인 여부
    started = ev("session_start")
    first_req = next((r for r in states if r.get("phase") == "REQUEST"), None)
    if started and first_req:
        out.append(("1 맥락 파악", "시작까지 시간", "✅",
                    f"{first_req['t_ms'] - started[0]['t_ms']} ms"))
    else:
        out.append(("1 맥락 파악", "시작까지 시간", "❌", "REQUEST 에 도달 못 함"))
    out.append(("1 맥락 파악", "재확인 여부", "✅" if utterances else "✅(0건)",
                sum(1 for u in utterances if u["intent"] == "repeat_request")))

    # 2. 과제 준비 — 개시 잠복시간 · 촉진 횟수
    out.append(("2 과제 준비", "개시 잠복시간", "❌",
                "가방을 놓는 동작 자체를 관측하지 않는다 (결정 3: 로봇이 연다)"))
    out.append(("2 과제 준비", "촉진 횟수", "✅", len(prompts)))

    # 3. 목표 부호화 — 재호명 요청 횟수
    repeats = [p for p in prompts if p.get("trigger") == "repeat_request"]
    out.append(("3 목표 부호화", "재호명 요청 횟수", "✅", len(repeats)))

    # 4. 시각적 탐색 — 탐색시간 · 비목표물 접촉
    lat = [r["latency_ms"] for r in responses if "latency_ms" in r]
    search = [r["search_ms"] for r in responses if r.get("search_ms") is not None]
    judge = [r["judge_ms"] for r in responses if r.get("judge_ms") is not None]
    out.append(("4 시각적 탐색", "탐색시간", "✅" if search else "❌",
                f"{search} ms  (판정 지연 {judge} ms 는 분리됨)"))
    out.append(("4 시각적 탐색", "비목표물 접촉", "❌", "책상에 올리기 전 접촉은 관측 밖"))

    # 5. 대조·선택 — 정확도 · 오선택률
    correct = sum(1 for r in responses if r.get("correct"))
    out.append(("5 대조·선택", "정확도", "✅",
                f"{correct}/{len(responses)}" if responses else "0/0"))
    wrong = [r for r in responses if r.get("outcome") == "incorrect"]
    out.append(("5 대조·선택", "오선택률", "✅",
                f"{len(wrong)}/{len(responses)}" if responses else "0/0"))
    off = [r for r in responses if r.get("on_target") is False]
    out.append(("5 대조·선택", "└ 호명 대상과 다른 것", "✅", len(off)))

    # 6. 피드백 처리 — 피드백 후 반응시간
    fb = []
    for i, r in enumerate(responses):
        if r.get("outcome") == "incorrect" and i + 1 < len(responses):
            fb.append(responses[i + 1]["t_ms"] - r["t_ms"])
    out.append(("6 피드백 처리", "피드백 후 반응시간", "✅" if fb else "✅(0건)", f"{fb} ms"))

    # 7. 오류 수정 — 자가 정정률 · 수정시간
    fixed = 0
    for i, r in enumerate(responses):
        if r.get("outcome") == "incorrect":
            nxt = next((n for n in responses[i + 1:] if n.get("requested") == r.get("requested")), None)
            if nxt and nxt.get("correct"):
                fixed += 1
    out.append(("7 오류 수정", "자가 정정률", "✅",
                f"{fixed}/{len(wrong)}" if wrong else "0/0"))
    fix_ms = []
    for i, r in enumerate(responses):
        if r.get("outcome") == "incorrect":
            nxt = next((n for n in responses[i + 1:]
                        if n.get("requested") == r.get("requested") and n.get("correct")), None)
            if nxt:
                fix_ms.append(nxt["t_ms"] - r["t_ms"])
    out.append(("7 오류 수정", "수정시간", "✅" if fix_ms else "✅(0건)", f"{fix_ms} ms"))

    # 8. 진행 점검 — 누락률 · 확인행동
    packed = [v for v in verifies if v.get("success")]
    out.append(("8 진행 점검", "누락률", "✅", f"담긴 {len(packed)} / 목록 3"))
    out.append(("8 진행 점검", "확인행동", "❌", "아동이 화면을 보는 행동은 관측 밖"))

    # 9. 반복·전환 — 전환시간 · 중도이탈
    reqs = [s_["t_ms"] for s_ in states if s_.get("phase") == "REQUEST"]
    gaps = [b - a for a, b in zip(reqs, reqs[1:])]
    out.append(("9 반복·전환", "전환시간", "✅" if gaps else "✅(0건)", f"{gaps} ms"))
    out.append(("9 반복·전환", "중도이탈", "✅",
                "END 도달" if any(s.get("phase") == "END" for s in states) else "미완주"))
    return out


def main() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        rows, phases = asyncio.run(run_session(Path(tmp)))

    print("=" * 78)
    print("1. 시나리오 흐름 — Ran 문서 ①~⑥")
    print("=" * 78)
    # NEXT 는 머무르지 않는 전이 상태라 태블릿으로 브로드캐스트되지 않는다.
    # 흐름 검사는 **로그의 state_change** 를 본다 — 그게 실제로 일어난 전이다.
    seen = set(phases) | {r.get("phase") for r in rows if r["event"] == "state_change"}
    missing = []
    for step, phase, what in SCRIPT:
        ok = phase in seen
        if not ok:
            missing.append(phase)
        print(f"  {step} {phase:12} {'✅' if ok else '❌'}  {what}")
    print(f"\n  관측된 전이: {' → '.join(phases)}")

    print()
    print("=" * 78)
    print("2. 하위과제별 행동지표 — Ran 문서 4절")
    print("=" * 78)
    rowsi = indicators(rows)
    for task, name, status, value in rowsi:
        print(f"  {status} {task:14} {name:22} {value}")

    ok = sum(1 for *_, s, _ in [(0, 0, s, v) for _, _, s, v in rowsi] if s.startswith("✅"))
    part = sum(1 for _, _, s, _ in rowsi if s.startswith("⚠️"))
    bad = sum(1 for _, _, s, _ in rowsi if s.startswith("❌"))
    print(f"\n  산출됨 {ok} · 부분 {part} · 못 냄 {bad}  (총 {len(rowsi)})")

    print()
    print("=" * 78)
    print("3. 로그 이벤트 분포")
    print("=" * 78)
    import collections
    for k, v in collections.Counter(r["event"] for r in rows).most_common():
        print(f"  {k:24} {v}")
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
