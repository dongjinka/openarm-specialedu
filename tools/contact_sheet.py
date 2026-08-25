#!/usr/bin/env python3
"""프레임 여러 장을 라벨 붙인 격자 한 장으로 묶는다.

라벨링할 때 이미지를 한 장씩 여는 것보다 훨씬 빠르고, 물체 간 색·형태 차이를
나란히 놓고 볼 수 있어 경계 사례를 잡기 좋다.

    python tools/contact_sheet.py --dir episode_labels/thumbs --out sheet.jpg --cols 5
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from PIL import Image, ImageDraw

EP = re.compile(r"ep_(\d+)")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", required=True)
    p.add_argument("--out", default="sheet.jpg")
    p.add_argument("--cols", type=int, default=5)
    p.add_argument("--cell", type=int, default=300, help="셀 가로 픽셀")
    p.add_argument("--pattern", default="*.jpg")
    p.add_argument("--crop", default=None,
                   help="l,t,r,b 비율 크롭 (예: 0.25,0.15,1.0,0.85) — 배치 구역만 보기")
    args = p.parse_args()

    paths = sorted(Path(args.dir).glob(args.pattern))
    if not paths:
        raise SystemExit(f"이미지 없음: {args.dir}/{args.pattern}")

    crop = [float(x) for x in args.crop.split(",")] if args.crop else None
    cols = args.cols
    rows = (len(paths) + cols - 1) // cols

    thumbs = []
    for path in paths:
        img = Image.open(path).convert("RGB")
        if crop:
            w, h = img.size
            img = img.crop((int(crop[0] * w), int(crop[1] * h),
                            int(crop[2] * w), int(crop[3] * h)))
        cw = args.cell
        ch = int(img.height * cw / img.width)
        thumbs.append((path, img.resize((cw, ch), Image.LANCZOS)))

    cell_h = max(t.height for _, t in thumbs) + 22
    sheet = Image.new("RGB", (cols * args.cell, rows * cell_h), "#111111")
    draw = ImageDraw.Draw(sheet)

    for i, (path, img) in enumerate(thumbs):
        x, y = (i % cols) * args.cell, (i // cols) * cell_h
        sheet.paste(img, (x, y + 22))
        m = EP.search(path.name)
        label = f"ep{m.group(1)}" if m else path.stem[:16]
        draw.rectangle([x, y, x + args.cell, y + 21], fill="#000000")
        draw.text((x + 6, y + 5), f"{label}  {path.stem.split('_t')[-1]}s", fill="#00ff88")

    sheet.save(args.out, quality=92)
    print(f"{len(thumbs)}장 → {args.out}  ({sheet.width}×{sheet.height})")


if __name__ == "__main__":
    main()
