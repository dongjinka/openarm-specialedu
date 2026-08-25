#!/usr/bin/env python3
"""새 씬(60epi) 평가 세트를 만든다.

`eval/labels_45.csv` 는 **120epi 씬** 라벨이라 지금 씬에 쓸 수 없다. 배치 구역의
위치도 물체 배열도 다르다. 그래서 새로 뽑는다.

라벨은 `eval/labels_60epi.csv` 에 있고, 프레임은 여기서 데이터셋 영상에서 뽑는다.
/tmp 는 재부팅으로 지워지므로 라벨만 리포에 남기고 이미지는 필요할 때 다시 만든다.

    python tools/extract_eval_set.py --out /tmp/eval60
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import openarm_env  # noqa: E402

openarm_env.load()

DATASET = "leapshared/nuedive_test_60epi_new_20260824_182026"
SCENE = "observation.images.follower_d455f"
#: moov 가 앞에 있어 앞부분만 받아도 디코딩된다. 5 에피소드(≈260초)면 90MB 로 충분하다.
PARTIAL_BYTES = 90_000_000


def token() -> str:
    p = Path.home() / ".cache" / "huggingface" / "token"
    if not p.is_file():
        raise SystemExit("HF 토큰이 없다: ~/.cache/huggingface/token")
    return p.read_text().strip()


def fetch(dest: Path) -> Path:
    import requests

    if dest.is_file() and dest.stat().st_size >= PARTIAL_BYTES:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://huggingface.co/datasets/{DATASET}/resolve/main/videos/{SCENE}/chunk-000/file-000.mp4"
    r = requests.get(url, headers={"Authorization": f"Bearer {token()}",
                                  "Range": f"bytes=0-{PARTIAL_BYTES}"}, timeout=300)
    r.raise_for_status()
    dest.write_bytes(r.content)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default=str(REPO / "eval" / "labels_60epi.csv"))
    ap.add_argument("--out", default="/tmp/eval60")
    ap.add_argument("--video", default="/tmp/ds60/scene000.mp4")
    args = ap.parse_args()

    rows = list(csv.DictReader(Path(args.labels).open(encoding="utf-8")))
    wanted = sorted({(r["thumb"], float(r["t_s"])) for r in rows}, key=lambda x: x[1])
    out = Path(args.out)
    (out / "thumbs").mkdir(parents=True, exist_ok=True)

    if all((out / name).is_file() for name, _ in wanted):
        print(f"이미 있다: {out} ({len(wanted)}장)")
        return 0

    print(f"영상 확보 중… ({args.video})")
    video = fetch(Path(args.video))

    import av
    from PIL import Image  # noqa: F401

    container = av.open(str(video))
    stream = container.streams.video[0]
    idx, made = 0, 0
    try:
        for frame in container.decode(stream):
            if idx >= len(wanted):
                break
            t = float(frame.pts * stream.time_base)
            while idx < len(wanted) and t >= wanted[idx][1]:
                frame.to_image().save(out / wanted[idx][0], quality=88)
                idx += 1
                made += 1
    except Exception as exc:  # noqa: BLE001
        print(f"디코드 중단: {type(exc).__name__} — {made}/{len(wanted)} 장까지")
    container.close()

    print(f"{made}장 → {out}")
    if made < len(wanted):
        print("⚠️ 일부를 못 뽑았다. PARTIAL_BYTES 를 늘려야 할 수 있다")
        return 1
    print(f"\n평가:  .venv/bin/python tools/eval_vlm.py --labels {args.labels} --root {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
