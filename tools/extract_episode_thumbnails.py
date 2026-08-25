#!/usr/bin/env python3
"""
LeRobot v3 데이터셋에서 에피소드별 대표 프레임을 추출하고
라벨링용 CSV 템플릿을 생성한다.

사용법:
    python extract_episode_thumbnails.py \
        --repo-id leapshared/neudive_test_120epi_0819_real_20260819_162506 \
        --out ./episode_labels

    # 로컬 캐시 경로를 직접 지정하는 경우
    python extract_episode_thumbnails.py --root ~/.cache/huggingface/lerobot/... --out ./episode_labels

출력:
    <out>/thumbs/ep_000.jpg ...        에피소드별 썸네일
    <out>/episode_object_map.csv       라벨링용 CSV (object 열이 비어 있음)
    <out>/label.html                   브라우저 라벨링 UI (같은 폴더에 복사해서 사용)
"""

import argparse
import csv
import sys
from pathlib import Path

import numpy as np
from PIL import Image


def load_dataset(repo_id: str | None, root: str | None):
    """LeRobot 버전에 따라 import 경로가 다르므로 방어적으로 로드."""
    LeRobotDataset = None
    errors = []
    for mod in (
        "lerobot.datasets.lerobot_dataset",
        "lerobot.common.datasets.lerobot_dataset",
    ):
        try:
            LeRobotDataset = __import__(mod, fromlist=["LeRobotDataset"]).LeRobotDataset
            break
        except Exception as e:  # noqa: BLE001
            errors.append(f"{mod}: {e}")
    if LeRobotDataset is None:
        print("LeRobotDataset import 실패:\n  " + "\n  ".join(errors), file=sys.stderr)
        sys.exit(1)

    kwargs = {}
    if root:
        kwargs["root"] = root
    return LeRobotDataset(repo_id, **kwargs)


def to_uint8_image(x) -> Image.Image:
    """LeRobot 이미지 텐서(CHW float 0~1 또는 HWC uint8)를 PIL로 변환."""
    arr = x.numpy() if hasattr(x, "numpy") else np.asarray(x)
    if arr.ndim == 3 and arr.shape[0] in (1, 3):  # CHW -> HWC
        arr = np.transpose(arr, (1, 2, 0))
    if arr.dtype != np.uint8:
        arr = (np.clip(arr, 0.0, 1.0) * 255).astype(np.uint8)
    if arr.shape[-1] == 1:
        arr = np.repeat(arr, 3, axis=-1)
    return Image.fromarray(arr)


def episode_ranges(ds):
    """에피소드별 (from_idx, to_idx)를 구한다. v3/v2 모두 대응."""
    ranges = {}

    # v3: meta.episodes 에 dataset_from_index / dataset_to_index
    eps = getattr(ds.meta, "episodes", None)
    if eps is not None:
        try:
            for i in range(len(eps)):
                row = eps[i]
                ep = int(row.get("episode_index", i))
                ranges[ep] = (int(row["dataset_from_index"]), int(row["dataset_to_index"]))
            if ranges:
                return ranges
        except Exception:  # noqa: BLE001
            ranges = {}

    # v2: episode_data_index
    edi = getattr(ds, "episode_data_index", None)
    if edi is not None:
        frm, to = edi["from"], edi["to"]
        for ep in range(len(frm)):
            ranges[ep] = (int(frm[ep]), int(to[ep]))
        if ranges:
            return ranges

    # 최후: 전체 스캔
    print("경고: 에피소드 경계 메타를 못 찾아 전체 스캔합니다 (느림)", file=sys.stderr)
    cur, start = None, 0
    for i in range(len(ds)):
        ep = int(ds[i]["episode_index"])
        if cur is None:
            cur = ep
        elif ep != cur:
            ranges[cur] = (start, i)
            cur, start = ep, i
    if cur is not None:
        ranges[cur] = (start, len(ds))
    return ranges


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--repo-id", default="leapshared/neudive_test_120epi_0819_real_20260819_162506")
    p.add_argument("--root", default=None, help="로컬 데이터셋 루트(선택)")
    p.add_argument("--out", default="./episode_labels")
    p.add_argument("--camera", default="observation.images.follower_d455f",
                   help="썸네일을 뽑을 카메라 키")
    p.add_argument("--frame-offset", type=int, default=5,
                   help="에피소드 시작으로부터 몇 번째 프레임을 쓸지 (팔이 물체를 가리기 전)")
    p.add_argument("--width", type=int, default=384, help="썸네일 가로 크기")
    args = p.parse_args()

    out = Path(args.out)
    thumbs = out / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)

    ds = load_dataset(args.repo_id, args.root)
    total_eps = getattr(ds.meta, "total_episodes", None)
    print(f"데이터셋 로드 완료 — total_episodes={total_eps}, total_frames={len(ds)}")
    if total_eps is not None and total_eps != 120:
        print(f"  ※ 주의: total_episodes가 {total_eps}입니다. 120이 아니면 메타 확인 필요.")

    if args.camera not in ds.meta.camera_keys:
        print(f"카메라 키 '{args.camera}' 없음. 사용 가능: {ds.meta.camera_keys}", file=sys.stderr)
        sys.exit(1)

    ranges = episode_ranges(ds)
    print(f"에피소드 {len(ranges)}개 발견")

    rows = []
    for ep in sorted(ranges):
        frm, to = ranges[ep]
        idx = min(frm + args.frame_offset, to - 1)
        try:
            img = to_uint8_image(ds[idx][args.camera])
        except Exception as e:  # noqa: BLE001
            print(f"  ep {ep}: 프레임 추출 실패 ({e})", file=sys.stderr)
            continue
        if args.width and img.width > args.width:
            h = int(img.height * args.width / img.width)
            img = img.resize((args.width, h), Image.LANCZOS)
        name = f"ep_{ep:03d}.jpg"
        img.save(thumbs / name, quality=88)
        rows.append({
            "episode_index": ep,
            "object": "",           # tree | flower | whale  ← 여기를 채움
            "frames": to - frm,
            "bag_state": "",        # empty | partial   (수집 시 가방 상태, 알면 기입)
            "thumb": f"thumbs/{name}",
            "notes": "",
        })
        if ep % 20 == 0:
            print(f"  ... ep {ep}")

    csv_path = out / "episode_object_map.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["episode_index", "object", "frames", "bag_state", "thumb", "notes"])
        w.writeheader()
        w.writerows(rows)

    print(f"\n완료: 썸네일 {len(rows)}장 → {thumbs}")
    print(f"      CSV 템플릿 → {csv_path}")
    print(f"\n다음 단계: label.html 을 {out} 안에 두고 브라우저로 열어 라벨링")


if __name__ == "__main__":
    main()
