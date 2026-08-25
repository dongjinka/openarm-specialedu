#!/usr/bin/env python3
"""AES 로 잠긴 zip 에서 `.env` 를 꺼낸다.

윈도우 탐색기 기본 압축 해제와 파이썬 `zipfile` 은 구형 ZipCrypto 만 지원해서
AES 로 잠긴 zip 을 못 푼다. 그래서 별도 도구가 필요하다.

비밀번호는 **인자로 받지 않는다** — 셸 히스토리와 `ps` 에 남기지 않기 위해서다.
꺼낸 내용도 화면에 찍지 않는다. 어떤 키가 들어왔는지 이름만 보여준다.

    python tools/unlock_env.py ~/Downloads/API_Key.zip
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("zip_path")
    ap.add_argument("--out", default=str(REPO / ".env"))
    ap.add_argument("--member", default=None, help="꺼낼 파일 (기본: 첫 번째)")
    args = ap.parse_args()

    try:
        import pyzipper
    except ImportError:
        return _fail("pyzipper 가 없다:  python3 -m pip --python .venv/bin/python install pyzipper")

    src = Path(args.zip_path).expanduser()
    if not src.is_file():
        return _fail(f"파일이 없다: {src}")

    out = Path(args.out)
    if out.exists():
        ans = input(f"{out} 가 이미 있다. 덮을까? [y/N] ").strip().lower()
        if ans != "y":
            print("그만둔다.")
            return 1

    password = getpass.getpass("zip 비밀번호: ").encode()

    try:
        with pyzipper.AESZipFile(src) as zf:
            names = zf.namelist()
            member = args.member or next((n for n in names if n.endswith(".env")), names[0])
            data = zf.read(member, pwd=password)
    except RuntimeError as exc:
        return _fail(f"열지 못했다 — 비밀번호가 다를 수 있다 ({exc})")
    except Exception as exc:  # noqa: BLE001
        return _fail(f"{type(exc).__name__}: {exc}")

    out.write_bytes(data)
    out.chmod(0o600)

    keys = [ln.split("=", 1)[0].strip() for ln in data.decode("utf-8", "replace").splitlines()
            if ln.strip() and not ln.strip().startswith("#") and "=" in ln]
    print(f"\n{member} → {out}  ({len(data)} 바이트, 권한 600)")
    print(f"들어온 키 {len(keys)}개: {', '.join(keys)}")
    print("\n값은 찍지 않았다. 확인은 tools/deploy_check.py 로 한다.")
    return 0


def _fail(msg: str) -> int:
    print(f"❌ {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
