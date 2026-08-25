#!/usr/bin/env python3
"""배포 대상에서 무엇이 되고 무엇이 안 되는지 **읽기만 해서** 보고한다.

아무것도 설치하지 않고 아무것도 고치지 않는다. leap 머신의 환경은 우리 것이 아니므로,
판단 재료만 내놓고 결정은 사람이 한다.

프로세스마다 필요한 것이 다르다:

    orchestrator · vlm_service · voice_service  →  순수 파이썬. 우리 venv 로 충분하다.
    robot_bridge                                →  lerobot · openarm_sciedu 가 필요해
                                                   **leap 머신의 venv 에서 돌아야 한다.**

    <leap-venv>/bin/python tools/deploy_check.py --robot     # 로봇 프로세스용 환경
    .venv/bin/python tools/deploy_check.py                   # 우리 venv 용 환경
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import os
import shutil
import socket
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import openarm_env  # noqa: E402

loaded = openarm_env.load()

CORE = [("fastapi", "허브 서버"), ("uvicorn", "허브 서버"), ("pydantic", "이벤트 계약"),
        ("websockets", "스포크 클라이언트"), ("httpx", "VLM·STT 호출"), ("PIL", "프레임 처리")]
VOICE = [("numpy", "오디오 버퍼"), ("sounddevice", "마이크")]
ROBOT = [("lerobot", "정책·롤아웃 엔진"), ("torch", "추론"),
         ("openarm_sciedu", "로봇 드라이버 + groot_frozen_bf16 등록"),
         ("websockets", "브릿지 클라이언트"), ("fastapi", "프레임 서버"),
         ("uvicorn", "프레임 서버"), ("PIL", "JPEG 인코딩"), ("numpy", "관측 변환")]

OK, BAD, WARN = "✅", "❌", "⚠️ "


def probe(mods) -> list[str]:
    missing = []
    for name, why in mods:
        found = importlib.util.find_spec(name) is not None
        print(f"  {OK if found else BAD} {name:18} {why}")
        if not found:
            missing.append(name)
    return missing


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--robot", action="store_true", help="robot_bridge 용 환경을 본다")
    args = ap.parse_args()

    print(f"python  {sys.version.split()[0]}  ({sys.executable})")
    print(f"repo    {REPO}\n")
    problems: list[str] = []

    if args.robot:
        print("robot_bridge 가 필요한 것 — 이 venv 는 leap 머신 것이어야 한다")
        missing = probe(ROBOT)
        if missing:
            problems.append(
                f"robot_bridge 의존성 없음: {missing}\n"
                "     lerobot·torch·openarm_sciedu 가 없으면 **여기는 그 머신이 아니다.**\n"
                "     나머지는 그 venv 에 추가해야 하는 것들이다 (그쪽 환경을 흔드니 신중히)."
            )
        # 정책 타입이 실제로 등록되는지 — 시연을 좌우한다.
        if importlib.util.find_spec("lerobot") is not None:
            print()
            try:
                for m in ("lerobot.policies.factory", "lerobot.policies"):
                    try:
                        importlib.import_module(m)
                        break
                    except Exception:
                        continue
                from lerobot.configs import PreTrainedConfig
                reg = getattr(PreTrainedConfig, "_choice_registry", {})
                have = "groot_frozen_bf16" in reg
                print(f"  {OK if have else BAD} groot_frozen_bf16 등록  "
                      f"(총 {len(reg)}종)")
                if not have:
                    problems.append(
                        "groot_frozen_bf16 가 등록되지 않았다.\n"
                        "     openarm_sciedu 플러그인이 등록한다 — 설치돼 있는지 확인.\n"
                        "     docs/GROOT_POLICY_DEPENDENCY.md 참고."
                    )
            except Exception as exc:  # noqa: BLE001
                problems.append(f"정책 레지스트리 조회 실패: {type(exc).__name__}: {exc}")
    else:
        print("orchestrator · vlm_service 가 필요한 것")
        missing = probe(CORE)
        if missing:
            problems.append(f"핵심 의존성 없음: {missing}  →  pip install -e '.[dev]'")

        print("\nvoice_service (선택 — 없으면 듣기만 꺼진 채 돈다)")
        if probe(VOICE):
            print(f"  {WARN}STT 없이 돈다. 설치하려면: pip install -e '.[voice]'")
            print(f"  {WARN}Linux 는 먼저: sudo apt install libportaudio2")

    print("\n포트")
    for port, who in ((8000, "orchestrator 허브"), (8081, "프레임 서버"), (4173, "태블릿")):
        free = port_free(port)
        print(f"  {OK if free else WARN}{port}  {who}{'' if free else '  — 이미 사용 중'}")

    print("\n환경변수")
    if loaded:
        print(f"  ({REPO / '.env'} 에서 {len(loaded)}개 읽음)")
    for key, why, needed in (("OPENAI_API_KEY", "VLM 판정", True),
                             ("VLM_PROVIDER", "백엔드 선택", False),
                             ("OPENAI_MODEL", "비전 모델", False),
                             ("CLOVA_SPEECH_SECRET", "STT (선택)", False),
                             ("HUMELO_API_KEY", "TTS 사전 생성 (선택)", False)):
        have = bool(os.environ.get(key))
        mark = OK if have else (BAD if needed else WARN)
        print(f"  {mark}{key:22} {why}")
        if needed and not have:
            problems.append(f"{key} 가 없다 — VLM 판정이 전부 실패한다.\n     cp .env.example .env 후 값을 채운다")

    print("\n시나리오·자산")
    scenario = REPO / "scenarios" / "minsu_playdate_v1.json"
    print(f"  {OK if scenario.is_file() else BAD} {scenario.relative_to(REPO)}")
    if not scenario.is_file():
        problems.append("시나리오 파일이 없다")
    node = shutil.which("node")
    print(f"  {OK if node else WARN}node  {node or '태블릿을 이 머신에서 서빙하려면 필요'}")

    print("\n" + "=" * 66)
    if problems:
        print(f"{BAD} 막는 것 {len(problems)}건\n")
        for i, p in enumerate(problems, 1):
            print(f"  {i}. {p}")
        return 1
    print(f"{OK} 이 프로세스를 띄울 준비가 됐다")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
