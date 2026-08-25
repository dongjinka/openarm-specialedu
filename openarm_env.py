"""`.env` 를 읽어 환경변수로 올린다. 의존성 없이.

키를 명령줄에 적으면 셸 히스토리와 `ps` 에 남는다. 프로세스가 넷이라 매번
export 하는 것도 실수하기 쉽다 — 하나만 빠뜨리면 그 서비스만 조용히 죽는다.

**이미 설정된 환경변수를 덮지 않는다.** 시연 현장에서 한 값만 임시로 바꿔야 할 때
`FOO=x python -m ...` 가 그대로 이긴다.
"""

from __future__ import annotations

import os
from pathlib import Path

#: 저장소 루트의 `.env`. `.gitignore` 에 들어 있다.
DEFAULT_PATH = Path(__file__).resolve().parent / ".env"


def load(path: str | Path | None = None, *, override: bool = False) -> dict[str, str]:
    """`KEY=VALUE` 줄을 읽어 환경에 넣는다. 넣은 것만 돌려준다.

    `#` 로 시작하는 줄과 빈 줄은 건너뛴다. 값의 따옴표는 벗긴다.
    """
    p = Path(path) if path else DEFAULT_PATH
    if not p.is_file():
        return {}

    applied: dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if not key or (key in os.environ and not override):
            continue
        os.environ[key] = value
        applied[key] = value
    return applied
