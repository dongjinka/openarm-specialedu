#!/usr/bin/env python3
"""고정 대본을 **미리** wav 로 만든다. 시연 전에 한 번 돌린다.

실행 중에 합성하지 않는 이유는 셋이다.
  · 한 문장에 약 5초가 걸린다 — 아동을 그만큼 기다리게 할 수 없다
  · 매 회차 같은 소리가 나야 한다 (예측 가능성)
  · TTS 서비스가 죽어도 세션이 돈다

벤더 프로필은 `openarm-sciedu/voice-pipeline/tts/config/*.json` 형식을 그대로 쓴다.
`${TEXT}` 자리에 문장이 들어가고 응답 본문이 곧 오디오다.
(배포된 `/v1/tts` 는 **재생만 하고 오디오를 안 준다** — 사전 생성에는 못 쓴다.)

소리는 태블릿에서 난다. 그래서 파일은 태블릿의 정적 자산 폴더에 떨어진다 —
`static/scenarios/toy-bag/audio/`. 빌드가 필요 없고 새로고침하면 바로 들린다.

    export HUMELO_API_KEY=...
    python tools/render_lines.py --profile <voice-pipeline>/tts/config/humelo_nana.json
    python tools/render_lines.py --profile ... --only robot_mistake --force
    python tools/render_lines.py --profile ... --include-human   # 목소리 통일
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

_VAR = re.compile(r"\$\{([A-Z_]+)\}")


def _subst(value, text: str):
    """`${TEXT}` 는 문장으로, 나머지 `${...}` 는 환경변수로 채운다."""
    if isinstance(value, str):
        def repl(m):
            key = m.group(1)
            if key == "TEXT":
                return text
            got = os.environ.get(key)
            if got is None:
                raise SystemExit(f"환경변수 {key} 가 없다 — 프로필이 요구한다")
            return got
        return _VAR.sub(repl, value)
    if isinstance(value, dict):
        return {k: _subst(v, text) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v, text) for v in value]
    return value


def render(profile: dict, text: str, timeout: float) -> bytes:
    import requests

    url = _subst(profile["batch_url"], text)
    headers = _subst(profile.get("headers", {}), text)
    body = _subst(profile.get("body", {}), text)
    r = requests.post(url, headers=headers, json=body, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} — {r.text[:200]}")
    if profile.get("response") == "audio":
        return r.content
    field = profile.get("audio_field")
    if not field:
        raise RuntimeError("프로필에 audio_field 가 없다")
    import base64
    return base64.b64decode(r.json()[field])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lines", default=str(REPO / "scenarios" / "utterances_ko.json"))
    ap.add_argument("--profile", required=True, help="벤더 프로필 JSON (배치 합성용)")
    ap.add_argument("--out", default=str(REPO.parent / "openarm-special-web" /
                                        "static" / "scenarios" / "toy-bag" / "audio"),
                    help="태블릿 자산 폴더. 태블릿이 소리를 내므로 파일이 거기 있어야 한다")
    ap.add_argument("--include-human", action="store_true",
                    help="사람 녹음이 있는 것까지 TTS 로 덮는다 (목소리를 하나로 통일할 때)")
    ap.add_argument("--only", action="append", help="이 발화만 (여러 번 줄 수 있다)")
    ap.add_argument("--force", action="store_true", help="이미 있어도 다시 만든다")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--dry-run", action="store_true", help="무엇을 만들지만 보여준다")
    args = ap.parse_args()

    spec = json.loads(Path(args.lines).read_text(encoding="utf-8"))
    profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    todo: list[tuple[str, str]] = []
    skipped_human = 0
    for uid, body in spec["utterances"].items():
        if args.only and uid not in args.only:
            continue
        for text, stem, source in zip(body["parts"], body["files"], body["source"]):
            # 사람 녹음은 덮지 않는다. 섞이는 것보다 낫지만, 목소리를 하나로 통일하고
            # 싶으면 --include-human 으로 전부 다시 만든다.
            if source == "human" and not args.include_human:
                skipped_human += 1
                continue
            name = f"{stem}.wav"
            if (out / name).is_file() and not args.force:
                continue
            todo.append((name, text))

    if skipped_human:
        print(f"사람 녹음 {skipped_human}개는 건너뛴다 (--include-human 으로 덮을 수 있다)")
    if not todo:
        print("만들 것이 없다 (이미 다 있음 — 다시 만들려면 --force)")
        return 0

    print(f"{len(todo)}개를 만든다 → {out}\n")
    for name, text in todo:
        print(f"  {name:28} {text}")
    if args.dry_run:
        return 0

    print()
    failed = []
    for name, text in todo:
        try:
            (out / name).write_bytes(render(profile, text, args.timeout))
            print(f"  ✅ {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, str(exc)[:120]))
            print(f"  ❌ {name}  {exc}")
    print()
    if failed:
        print(f"실패 {len(failed)}건 — 그 발화는 자막만 나가고 넘어간다")
        return 1
    print(f"완료 → {out}")
    print("태블릿을 새로고침하면 바로 들린다 (정적 자산이라 빌드가 필요 없다)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
