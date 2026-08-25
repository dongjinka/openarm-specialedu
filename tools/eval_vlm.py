#!/usr/bin/env python3
"""VLM 판별 평가 (부록 B).

두 정확도를 **분리 측정**한다. 전체 정확도 하나만 보면 약한 물체가 묻힌다.

  1. 물체 인식  — tree/flower/whale/other/none 혼동행렬
  2. 호출 판단  — 체크리스트를 주입했을 때의 `should_pack` 정확도

특히 중요한 것은 **방해자극을 `other` 로 거르는 것**이다. 이걸 셋 중 하나로
잘못 넣으면 로봇이 엉뚱한 물건을 집는다. `other → tree/flower/whale` 오분류를
따로 세어 보고한다.

    python tools/eval_vlm.py --labels episode_labels/episode_object_map.csv \
        --root episode_labels --checklist flower,whale,tree
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vlm_service.backends import make_backend           # noqa: E402
from vlm_service.contract import ObjectClass, decide    # noqa: E402
from vlm_service.service import JudgeService            # noqa: E402

CLASSES = [c.value for c in ObjectClass]


def load_labels(path: Path, root: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            truth = (row.get("object") or "").strip().lower()
            if not truth:
                continue                       # 라벨이 비어 있으면 평가 대상이 아니다
            if truth not in CLASSES:
                print(f"  ! 알 수 없는 라벨 '{truth}' — 건너뜀", file=sys.stderr)
                continue
            image = root / row["thumb"]
            if not image.exists():
                print(f"  ! 이미지 없음: {image}", file=sys.stderr)
                continue
            rows.append({**row, "truth": truth, "image": image})
    return rows


def matrix(pairs: list[tuple[str, str]]) -> str:
    seen = [c for c in CLASSES if any(c in p for p in pairs)]
    counts: dict[tuple[str, str], int] = Counter(pairs)
    width = max(len(c) for c in seen) + 2
    head = "실제\\예측".ljust(width) + "".join(c.rjust(width) for c in seen) + "   정확도"
    lines = [head, "-" * len(head)]
    for t in seen:
        row = [counts.get((t, p), 0) for p in seen]
        total = sum(row)
        acc = f"{counts.get((t, t), 0) / total * 100:6.1f}%" if total else "     -"
        lines.append(t.ljust(width) + "".join(str(v).rjust(width) for v in row) + f"  {acc}")
    return "\n".join(lines)


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--labels", required=True)
    p.add_argument("--root", default=None, help="thumb 경로의 기준 디렉터리")
    p.add_argument("--checklist", default="flower,whale,tree")
    p.add_argument("--packed", default="", help="이미 담은 항목 (쉼표 구분)")
    p.add_argument("--provider", default=None)
    p.add_argument("--min-confidence", type=float, default=0.70)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--out", default=None, help="상세 결과 JSON 경로")
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    labels_path = Path(args.labels)
    root = Path(args.root) if args.root else labels_path.parent
    rows = load_labels(labels_path, root)
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        sys.exit("라벨이 채워진 행이 없다. label.html 로 먼저 라벨링할 것.")

    checklist = [x.strip() for x in args.checklist.split(",") if x.strip()]
    packed = [x.strip() for x in args.packed.split(",") if x.strip()]

    backend = make_backend(args.provider)
    service = JudgeService(backend, min_confidence=args.min_confidence)
    print(f"백엔드: {backend.name}  |  샘플 {len(rows)}장  |  체크리스트 {checklist}\n",
          file=sys.stderr)

    sem = asyncio.Semaphore(args.concurrency)
    results: list[dict] = []

    async def run(row: dict) -> None:
        import time as _t
        async with sem:
            t0 = _t.monotonic()
            perception = await service.perceive(row["image"].read_bytes(), checklist, packed)
            latency = _t.monotonic() - t0
        verdict = decide(perception, checklist, packed, min_confidence=args.min_confidence)
        truth_should_pack = row["truth"] in [o for o in checklist if o not in packed]
        results.append({
            "episode_index": row.get("episode_index"),
            "thumb": row["thumb"],
            "truth": row["truth"],
            "pred": perception.object.value,
            "confidence": perception.confidence,
            "reason": perception.reason,
            "should_pack_pred": verdict.should_pack,
            "should_pack_truth": truth_should_pack,
            "hold_for_operator": verdict.hold_for_operator,
            "latency_s": round(latency, 3),
        })
        mark = "✓" if perception.object.value == row["truth"] else "✗"
        print(f"  {mark} ep{row.get('episode_index','?'):>4}  실제={row['truth']:<7} "
              f"예측={perception.object.value:<7} conf={perception.confidence:.2f}  "
              f"{perception.reason[:44]}", file=sys.stderr)

    await asyncio.gather(*(run(r) for r in rows))
    results.sort(key=lambda r: int(r["episode_index"] or 0))

    pairs = [(r["truth"], r["pred"]) for r in results]
    correct = sum(1 for t, pr in pairs if t == pr)
    sp_correct = sum(1 for r in results if r["should_pack_pred"] == r["should_pack_truth"])

    print("\n" + "=" * 64)
    print("1. 물체 인식 — 혼동행렬")
    print("=" * 64)
    print(matrix(pairs))
    print(f"\n전체 정확도: {correct}/{len(pairs)} = {correct/len(pairs)*100:.1f}%")

    per = defaultdict(lambda: [0, 0])
    for t, pr in pairs:
        per[t][1] += 1
        per[t][0] += t == pr
    print("\n물체별 (목표 95%+):")
    for cls, (ok, total) in sorted(per.items()):
        flag = "" if total and ok / total >= 0.95 else "   ← 미달"
        print(f"  {cls:<7} {ok:>3}/{total:<3} {ok/total*100:5.1f}%{flag}")

    leaked = [r for r in results
              if r["truth"] == "other" and r["pred"] in ("tree", "flower", "whale")]
    print(f"\n★ 방해자극 누출 (other → 체크리스트 물건): {len(leaked)}건")
    if leaked:
        print("  로봇이 엉뚱한 물건을 집는 경로다. 0 이어야 한다.")
        for r in leaked:
            print(f"    ep{r['episode_index']} → {r['pred']} (conf {r['confidence']:.2f})")

    print("\n" + "=" * 64)
    print("2. 호출 판단 — should_pack 정확도")
    print("=" * 64)
    print(f"  {sp_correct}/{len(results)} = {sp_correct/len(results)*100:.1f}%")
    held = sum(1 for r in results if r["hold_for_operator"])
    print(f"  운영자 보류: {held}/{len(results)} ({held/len(results)*100:.1f}%)")
    lat = sorted(r["latency_s"] for r in results)
    n = len(lat)
    print(f"\n지연 (동시 {args.concurrency}): 중앙 {lat[n//2]:.2f}s / "
          f"평균 {sum(lat)/n:.2f}s / p95 {lat[min(n-1, int(n*0.95))]:.2f}s / 최대 {lat[-1]:.2f}s")
    if args.concurrency == 1:
        budget = sum(1 for x in lat if x <= 3.0)
        print(f"T_judge ≤ 3초 예산 충족: {budget}/{n} = {budget/n*100:.0f}%")

    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"상세 결과 → {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
