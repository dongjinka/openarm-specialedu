"""변화 구동 감시 루프 테스트 (§C-1).

핵심 불변식: **변화가 없으면 VLM 을 부르지 않는다.** 이것 하나로 "오답 물건이 책상에
남아 같은 판정을 무한 반복"하는 문제가 사라진다. 아이가 물건을 치우면 변화가 생기고,
그때 나온 `none` 이 감시를 다시 무장시킨다.

프레임은 합성한다 — 데이터셋을 받아야 도는 테스트는 회귀 테스트가 못 된다.
임계값 자체는 실측으로 정했고 근거는 `vlm_service/watcher.py` 에 적어뒀다.
"""

from __future__ import annotations

import asyncio
import io

import pytest
from PIL import Image

from vlm_service import watcher as W
from vlm_service.watcher import ZoneWatcher


def frame(*, obj: bool = False, shade: int = 200) -> bytes:
    """빈 책상 / 물체가 놓인 책상. 물체는 큰 사각형이라 차분이 확실히 임계값을 넘는다."""
    img = Image.new("RGB", (320, 240), (shade, shade, shade))
    if obj:
        Image.Image.paste(img, Image.new("RGB", (90, 70), (20, 90, 200)), (200, 150))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


EMPTY, OBJECT, OTHER = frame(), frame(obj=True), frame(obj=True, shade=120)


class Frames:
    """호출할 때마다 다음 프레임을 준다. 목록이 끝나면 마지막 프레임을 계속 준다."""

    name = "fake"

    def __init__(self, seq):
        self.seq = list(seq)
        self.i = -1

    async def latest(self) -> bytes:
        self.i = min(self.i + 1, len(self.seq) - 1)
        return self.seq[self.i]


class Recorder:
    def __init__(self, verdicts):
        self.verdicts = verdicts        # bytes -> payload(dict) 또는 None
        self.calls: list[bytes] = []
        self.emitted: list[dict] = []

    async def judge(self, data: bytes):
        self.calls.append(data)
        return self.verdicts.get(data)

    async def emit(self, payload: dict) -> None:
        self.emitted.append(payload)

    @property
    def types(self) -> list[str]:
        return [e["type"] for e in self.emitted]


async def run(seq, verdicts, *, mode="judge", ticks=40):
    """틱 로직만 격리해 돌린다.

    `set_mode` 는 백그라운드 루프를 띄우고 그 루프는 무장 직후 1회를 조건 없이 판정한다
    (의도된 동작 — `test_arming_evaluates_once_immediately` 에서 따로 검증한다).
    여기서는 그 초기 판정을 섞지 않고 변화 감지 자체만 본다.
    """
    rec = Recorder(verdicts)
    w = ZoneWatcher(Frames(seq), rec.judge, rec.emit)
    w.mode = mode                      # 루프를 띄우지 않는다
    for _ in range(ticks):
        await w._tick()
    return rec, w


JUDGED = {"type": "judge", "object": "whale", "should_pack": True, "confidence": 0.98}


@pytest.mark.asyncio
async def test_settled_object_triggers_exactly_one_child_placed():
    seq = [EMPTY, EMPTY, OBJECT] + [OBJECT] * 12
    rec, _ = await run(seq, {OBJECT: JUDGED, EMPTY: None})
    assert rec.types.count("child_placed") == 1, rec.types
    # 정지한 뒤 계속 같은 장면이면 다시 부르지 않는다.
    assert rec.calls.count(OBJECT) == 1


@pytest.mark.asyncio
async def test_static_scene_never_calls_the_model():
    """오답 물건이 그대로 놓여 있는 상황. 변화가 없으니 호출도 없어야 한다."""
    rec, _ = await run([OBJECT] * 20, {OBJECT: JUDGED})
    assert rec.calls == [], "변화가 없는데 VLM 을 불렀다 — 오답 무한 반복의 원인"
    assert rec.emitted == []


@pytest.mark.asyncio
async def test_transient_motion_that_settles_back_does_not_call_the_model():
    """아이가 손을 뻗었다가 아무것도 놓지 않고 물러났다.

    움직임은 있었지만 장면은 그대로다. 무장 시점과 견줘 달라진 게 없으면 부르지 않는다 —
    그렇지 않으면 아이가 망설일 때마다 호출이 쌓인다.
    """
    seq = [EMPTY, EMPTY, OTHER, OTHER, EMPTY] + [EMPTY] * 12   # 지나갔다 제자리
    rec, _ = await run(seq, {EMPTY: None, OTHER: JUDGED})
    assert rec.calls == [], f"제자리로 돌아왔는데 호출했다: {len(rec.calls)}회"
    assert rec.emitted == []


@pytest.mark.asyncio
async def test_removing_the_object_rearms_without_triggering():
    """아이가 오답 물건을 치웠다 → none → child_placed 없이 다시 무장 → 새 물건은 잡는다."""
    seq = ([OBJECT] * 3 + [EMPTY] * 8 + [OTHER] * 10)
    rec, _ = await run(seq, {EMPTY: None, OTHER: JUDGED, OBJECT: JUDGED})
    assert rec.types.count("child_placed") == 1, rec.types
    assert EMPTY in rec.calls, "치운 것을 확인하지 않았다"
    assert OTHER in rec.calls, "새로 올린 물건을 판정하지 않았다"


@pytest.mark.asyncio
async def test_guard_mode_never_calls_the_model_and_alerts_once():
    seq = [EMPTY, EMPTY, OBJECT] + [OBJECT] * 10 + [OTHER] * 12
    rec, _ = await run(seq, {OBJECT: JUDGED, OTHER: JUDGED}, mode="guard", ticks=60)
    assert rec.calls == [], "guard 는 VLM 을 부르면 안 된다"
    # 프레임 차분은 팔 움직임과 물건을 가르지 못한다. 그래서 턴당 한 번만 부른다.
    assert rec.types.count("zone_disturbed") == 1, rec.types


@pytest.mark.asyncio
async def test_cache_answers_a_following_judge_request():
    seq = [EMPTY, EMPTY, OBJECT] + [OBJECT] * 10
    rec, w = await run(seq, {OBJECT: JUDGED, EMPTY: None})
    assert w.cached() == JUDGED, "직후 judge_request 가 캐시를 못 쓰면 호출이 2회가 된다"


@pytest.mark.asyncio
async def test_cache_expires_so_a_stale_verdict_is_never_reused():
    seq = [EMPTY, EMPTY, OBJECT] + [OBJECT] * 10
    rec, w = await run(seq, {OBJECT: JUDGED, EMPTY: None})
    w.cache.at -= (w.cache_ttl_s + 1)
    assert w.cached() is None


@pytest.mark.asyncio
async def test_arming_evaluates_once_immediately():
    """무장 직후 1회는 변화를 기다리지 않고 본다.

    로봇 턴(46초) 동안 아이가 다음 물건을 미리 올려두면 변화 이벤트가 없어
    변화 구동 감시는 영영 깨어나지 않는다. 그 잔여분을 여기서 회수한다.
    """
    rec = Recorder({OBJECT: JUDGED})
    w = ZoneWatcher(Frames([OBJECT]), rec.judge, rec.emit)
    w.set_mode("judge")                # 루프를 띄운다
    await asyncio.sleep(0.05)
    await w.aclose()
    assert rec.types == ["child_placed"], rec.types


@pytest.mark.asyncio
async def test_mode_off_stops_the_loop():
    rec = Recorder({OBJECT: JUDGED})
    w = ZoneWatcher(Frames([EMPTY, OBJECT]), rec.judge, rec.emit)
    w.set_mode("judge")
    await asyncio.sleep(0)
    w.set_mode("off")
    assert w._task is None
    await asyncio.sleep(0.05)
    await w.aclose()


@pytest.mark.asyncio
async def test_low_confidence_still_reaches_the_operator():
    """보류를 판정처럼 흘려야 한다.

    `vlm_hold` 를 감시 쪽에서 직접 보내고 `child_placed` 를 생략하면, 오케스트레이터는
    WAIT_CHILD 에 머무는데 `vlm_hold` 는 JUDGE 에서만 처리된다 — 이벤트가 조용히
    버려지고 **운영자가 호출되지 않는다.** 실측 보류는 45장 중 15건이었다.
    """
    HOLD = {"type": "vlm_hold", "object": "other", "confidence": 0.4, "reason": "체크리스트 밖"}
    seq = [EMPTY, EMPTY, OBJECT] + [OBJECT] * 10
    rec, w = await run(seq, {OBJECT: HOLD, EMPTY: None})
    assert rec.types.count("child_placed") == 1, "보류가 트리거를 만들지 않았다"
    assert w.cached() == HOLD, "보류 payload 가 캐시되지 않아 JUDGE 가 다시 호출한다"
