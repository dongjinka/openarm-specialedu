"""에피소드(=아동 1회 활동) 단위 JSONL 로거.

§7 이 요구한 "대표님의 현재 기능 평가"용 근거 데이터의 원본이다. 필수 필드는
정/오반응, 반응 시간, 재시도 횟수, 프롬프트 레벨, 독립 수행 여부.

`ts` 외에 **단조 증가 `seq`** 를 같이 적는다. 벽시계만으로는 같은 밀리초에 들어온
이벤트의 순서가 복원되지 않아 로그 리플레이가 깨진다.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any


class EpisodeLogger:
    def __init__(self, session_id: str, log_dir: str | Path = "logs") -> None:
        self.session_id = session_id
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{session_id}.jsonl"
        self._seq = 0
        # 세션 기준 단조 시계. 벽시계는 사람이 읽으라고 있고, **지표 계산은 이걸로 한다.**
        # Ran 문서 4절의 지표 다섯 개가 시간 기반이다 — 개시 잠복시간 · 탐색시간 ·
        # 수정시간 · 전환시간 · 피드백 후 반응시간. 초 해상도로는 하나도 못 낸다.
        self._t0 = time.monotonic()
        self._fh = self.path.open("a", encoding="utf-8")

    #: 호출자가 덮어쓸 수 없는 봉투 키. `fields` 에 같은 이름이 오면 밀어낸다 —
    #: 조용히 덮이면 평가 근거 데이터가 소리 없이 오염된다.
    RESERVED = ("ts", "t_ms", "seq", "session_id", "event")

    def write(self, event: str, fields: dict[str, Any] | None = None) -> dict[str, Any]:
        self._seq += 1
        payload = dict(fields or {})
        shadowed = sorted(k for k in payload if k in self.RESERVED)
        for key in shadowed:
            payload.pop(key)
        record = {
            # 밀리초까지 남긴다. 초 단위로 자르면 같은 초 안의 순서가 사라지고,
            # 그 순간 지표의 분모가 0 이 된다.
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            #: 세션 시작 기준 경과 ms. 벽시계 보정·서머타임과 무관하다.
            "t_ms": int((time.monotonic() - self._t0) * 1000),
            "seq": self._seq,
            "session_id": self.session_id,
            "event": event,
            **payload,
        }
        if shadowed:
            record["_shadowed"] = shadowed
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()
        return record

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()
