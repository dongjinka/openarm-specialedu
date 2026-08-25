"""STT 텍스트 → 의도.

`stt-eval` 이 도달한 결론을 그대로 따른다:

> 주지표는 분류 정확도이고 CER 은 진단용 보조지표다. STT 텍스트는 분류기로만
> 들어가므로, 전사가 틀려도 분류가 맞으면 시스템은 정상이다.

그래서 **정확 매칭(rule)에 편집거리 보정(fuzzy)을 얹는다.** 아동 발음과 CLOVA 오인식이
겹치면 정확 매칭만으로는 대부분 놓친다.

의도 집합은 작고 닫혀 있다. 넓히고 싶은 유혹을 참는 이유는, 열린 집합이 되는 순간
"무엇을 말했는가"가 아니라 "무엇을 하고 싶은가"를 추측하기 시작하고, 그 추측이
틀리면 아동이 원치 않는 상태 전이가 일어나기 때문이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from orchestrator.events import Intent

#: 의도별 표지 어구. 띄어쓰기를 지운 형태로 비교하므로 여기서도 붙여 적을 필요는 없다.
KEYWORDS: dict[Intent, tuple[str, ...]] = {
    Intent.REPEAT_REQUEST: (
        "뭐라고", "뭐라구", "뭐라했어", "다시말해", "다시한번", "한번더", "또말해",
        "못들었어", "안들려", "뭐가져와", "뭐였지", "무슨장난감", "뭐찾아",
    ),
    Intent.DONT_KNOW: (
        "모르겠어", "모르겠", "몰라", "어디있어", "어디있지", "못찾겠어", "못찾겠",
        "안보여", "어려워", "어떻게해",
    ),
    Intent.BREAK: (
        "쉬고싶어", "쉬고싶", "쉴래", "그만할래", "그만", "하기싫어", "하기싫",
        "힘들어", "안할래",
    ),
    Intent.DONE: (
        "다했어", "다했다", "여기있어", "여기요", "가져왔어", "찾았어", "찾았다", "했어요",
    ),
}

#: 편집거리를 이 비율 이하로 낮추면 같은 어구로 본다. 짧은 어구일수록 엄격해진다.
FUZZY_RATIO = 0.34
#: 이보다 짧은 표지는 퍼지 매칭에 쓰지 않는다 — "그만" 같은 2음절이 아무 데나 붙는다.
MIN_FUZZY_LEN = 3

_STRIP = re.compile(r"[\s.,!?~…·\"'()\[\]]+")


def normalize(text: str) -> str:
    return _STRIP.sub("", (text or "")).lower()


def edit_distance(a: str, b: str) -> int:
    """레벤슈타인. 문장이 짧아 순수 파이썬으로 충분하다."""
    if a == b:
        return 0
    if not a or not b:
        return len(a) or len(b)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


@dataclass(frozen=True)
class Decision:
    intent: Intent
    confidence: float
    matched: str = ""
    how: str = ""


def _rule(packed: str) -> tuple[Intent, str] | None:
    """정확 부분문자열. 가장 긴 표지를 이긴 것으로 친다 — '그만' 보다 '그만할래' 가 낫다."""
    best: tuple[int, Intent, str] | None = None
    for intent, words in KEYWORDS.items():
        for w in words:
            key = normalize(w)
            if key and key in packed and (best is None or len(key) > best[0]):
                best = (len(key), intent, key)
    return (best[1], best[2]) if best else None


def _fuzzy(packed: str) -> tuple[Intent, str, float] | None:
    """편집거리로 오인식을 회수한다.

    표지와 길이가 비슷한 **부분 구간**만 본다. 문장 전체와 비교하면 긴 발화에서
    거리가 항상 커져 아무것도 안 잡힌다.
    """
    best: tuple[float, Intent, str] | None = None
    for intent, words in KEYWORDS.items():
        for w in words:
            key = normalize(w)
            if len(key) < MIN_FUZZY_LEN:
                continue
            span = len(key)
            for start in range(0, max(1, len(packed) - span + 2)):
                window = packed[start:start + span]
                if not window:
                    continue
                ratio = edit_distance(window, key) / span
                if ratio <= FUZZY_RATIO and (best is None or ratio < best[0]):
                    best = (ratio, intent, key)
    if best is None:
        return None
    ratio, intent, key = best
    return intent, key, 1.0 - ratio


def classify(text: str) -> Decision:
    """텍스트 → 의도. 못 알아들으면 `other` 다 — 추측하지 않는다."""
    packed = normalize(text)
    if not packed:
        return Decision(Intent.OTHER, 0.0, how="빈 인식 결과")

    hit = _rule(packed)
    if hit:
        return Decision(hit[0], 0.95, matched=hit[1], how="rule")

    soft = _fuzzy(packed)
    if soft:
        intent, key, conf = soft
        return Decision(intent, round(conf * 0.9, 3), matched=key, how="fuzzy")

    return Decision(Intent.OTHER, 0.0, how="표지 없음")
