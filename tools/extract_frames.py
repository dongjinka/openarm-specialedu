#!/usr/bin/env python3
"""LeRobot v3 데이터셋에서 **필요한 프레임만** 뽑는다.

`extract_episode_thumbnails.py` 는 썸네일 120장을 위해 LeRobotDataset 으로 전체
영상(카메라 3대 × 110,929프레임, 수십 GB)을 받는다. 이 도구는 `meta/episodes/*.parquet`
의 (file_index, from_timestamp) 를 읽어 **해당 영상 파일만 받고 그 지점만 디코드**한다.

    python tools/extract_frames.py --episodes 38-44 --out episode_labels
    python tools/extract_frames.py --file-index 2 --offsets 0.2,8,16 --out frames

출력: <out>/thumbs/ep_XXX_tYYY.jpg  +  <out>/episode_object_map.csv (라벨 비어 있음)
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import av
import pandas as pd
import requests

REPO = "leapshared/neudive_test_120epi_0819_real_20260819_162506"
BASE = "https://huggingface.co/datasets/{repo}/resolve/main"
SCENE = "observation.images.follower_d455f"


def fetch(url: str, dest: Path) -> Path:
    """이미 있으면 다시 받지 않는다."""
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        got = 0
        with tmp.open("wb") as fh:
            for block in r.iter_content(chunk_size=1 << 20):
                fh.write(block)
                got += len(block)
                if total:
                    pct = got * 100 // total
                    print(f"\r  {dest.name}: {pct:3d}% ({got/1e6:.0f}/{total/1e6:.0f} MB)",
                          end="", file=sys.stderr)
    print(file=sys.stderr)
    tmp.rename(dest)
    return dest


def load_episodes(cache: Path) -> pd.DataFrame:
    import pyarrow.parquet as pq

    frames = []
    for i in range(2):
        url = f"{BASE.format(repo=REPO)}/meta/episodes/chunk-000/file-{i:03d}.parquet"
        path = fetch(url, cache / f"meta_ep_{i:03d}.parquet")
        frames.append(pq.read_table(path).to_pandas())
    return pd.concat(frames, ignore_index=True).sort_values("episode_index")


def parse_episodes(spec: str | None, df: pd.DataFrame) -> list[int]:
    if not spec:
        return sorted(df["episode_index"].tolist())
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def grab(video: Path, timestamps: list[float]) -> dict[float, "av.VideoFrame"]:
    """지정한 절대 타임스탬프들에서 가장 가까운 프레임을 뽑는다."""
    got: dict[float, av.VideoFrame] = {}
    with av.open(str(video)) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        for want in sorted(timestamps):
            offset = int(want / stream.time_base)
            try:
                container.seek(offset, stream=stream, backward=True, any_frame=False)
            except av.AVError:
                container.seek(0, stream=stream)
            for frame in container.decode(stream):
                if frame.time is not None and frame.time >= want - 1e-3:
                    got[want] = frame
                    break
    return got


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--episodes", default=None, help="예: 0-18 또는 3,7,12 (기본: 전부)")
    p.add_argument("--file-index", type=int, default=None, help="이 영상 파일의 에피소드만")
    p.add_argument("--camera", default=SCENE)
    p.add_argument("--offsets", default="0.2",
                   help="에피소드 시작으로부터의 초 단위 오프셋들 (쉼표 구분)")
    p.add_argument("--out", default="episode_labels")
    p.add_argument("--cache", default=".cache/frames")
    p.add_argument("--width", type=int, default=512)
    args = p.parse_args()

    cache = Path(args.cache)
    out = Path(args.out)
    thumbs = out / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)

    df = load_episodes(cache)
    fi_col = f"videos/{args.camera}/file_index"
    ci_col = f"videos/{args.camera}/chunk_index"
    ts_col = f"videos/{args.camera}/from_timestamp"

    if args.file_index is not None:
        df = df[df[fi_col] == args.file_index]
    wanted = set(parse_episodes(args.episodes, df))
    df = df[df["episode_index"].isin(wanted)]
    if df.empty:
        sys.exit("해당하는 에피소드가 없다")

    offsets = [float(x) for x in args.offsets.split(",")]
    rows = []

    for (chunk, fidx), group in df.groupby([ci_col, fi_col]):
        url = (f"{BASE.format(repo=REPO)}/videos/{args.camera}/"
               f"chunk-{int(chunk):03d}/file-{int(fidx):03d}.mp4")
        video = fetch(url, cache / f"{args.camera}_c{int(chunk):03d}_f{int(fidx):03d}.mp4")
        print(f"[file-{int(fidx):03d}] 에피소드 {len(group)}개", file=sys.stderr)

        targets: dict[float, tuple[int, float]] = {}
        for _, row in group.iterrows():
            base = float(row[ts_col])
            dur = int(row["length"]) / 30.0
            for off in offsets:
                targets[base + min(off, max(dur - 0.5, 0))] = (int(row["episode_index"]), off)

        for ts, frame in grab(video, list(targets)).items():
            ep, off = targets[ts]
            img = frame.to_image()
            if args.width and img.width > args.width:
                img = img.resize((args.width, int(img.height * args.width / img.width)))
            name = f"ep_{ep:03d}_t{off:g}.jpg"
            img.save(thumbs / name, quality=90)
            rows.append({"episode_index": ep, "offset_s": off, "object": "",
                         "bag_state_at_start": "", "thumb": f"thumbs/{name}", "notes": ""})

    rows.sort(key=lambda r: (r["episode_index"], r["offset_s"]))
    csv_path = out / "episode_object_map.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["episode_index", "offset_s", "object",
                                           "bag_state_at_start", "thumb", "notes"])
        w.writeheader()
        w.writerows(rows)
    print(f"\n프레임 {len(rows)}장 → {thumbs}\nCSV → {csv_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
