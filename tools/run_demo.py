#!/usr/bin/env python3
"""시연 런처 — 이 파일 하나만 실행하면 전 구간이 뜬다.

    python3 tools/run_demo.py            # 실물 로봇
    python3 tools/run_demo.py --sim      # 하드웨어 없이 (리허설·소프트웨어 확인)

사람이 하는 일은 **딱 하나** — 태블릿 화면에 뜬 4자리 코드를 입력하는 것.
코드는 `/api/session` 이 매번 새로 발급하므로 미리 받아둘 수 없다(태블릿이
표시한 그 코드여야 한다). 그래서 이 단계만 손이 필요하다.

기동 순서는 고정이다. 각 단계는 **로그로 준비 완료를 확인한 뒤** 다음으로 넘어간다 —
`sleep` 으로 넘기면 GPU 로드가 늦어진 날 조용히 어긋난다.

    0. 잔여 프로세스 검사 (같은 CAN 버스를 두 프로세스가 잡으면 팔 핸드셰이크가 통째로 실패한다)
    1. orchestrator          → /health 200
    2. robot_bridge          → "오케스트레이터 연결됨" · 실물이면 "GR00T 준비 완료" 까지
    3. vlm_service           → "연결됨"
    4. (사람) 태블릿 코드 입력
    5. cloud_bridge          → "pair:established"

종료는 Ctrl-C. 역순으로 정리하고 robot_bridge 에는 teardown 시간을 준다.

⚠️ 이 스크립트는 **실물 팔을 움직인다.** `--real` 은 사람이 지켜보는 상태에서만 쓴다.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[1]
VENV_PY = REPO / ".venv" / "bin" / "python"

#: `--real` 은 lerobot·GR00T 플러그인이 설치된 openarmsciedu 쪽 인터프리터로 돌려야 한다.
#: 그 프로젝트의 파일은 읽기만 한다(수정 금지) — 우리 코드는 PYTHONPATH 로 얹는다.
SCIEDU_PY = pathlib.Path("/home/leap/Documents/openarmsciedu/.venv/bin/python3")
#: 그 인터프리터에 `websockets` 가 없다. 우리 리포 안에 벤더링해 두고 PYTHONPATH 로 준다.
VENDOR = REPO / "vendor"

POLICY_GLOB = str(pathlib.Path.home() / ".cache/huggingface/hub"
                  / "models--leapshared--nuedive_test_90epi_merged_20260825_GR00T17"
                  / "snapshots" / "*")
ROBOT_CONFIG = REPO / "config" / "robot_real.json"

HUB = "ws://127.0.0.1:8000"
HEALTH = "http://127.0.0.1:8000/health"
CLOUD = "wss://openarm-special-web.neudive7.workers.dev"
SESSION_API = "https://openarm-special-web.neudive7.workers.dev/api/session"

LOG_DIR = pathlib.Path("/tmp/demo")

#: 기동 중 이 문자열이 보이면 즉시 실패로 처리한다. 조용히 넘어가면 원인이 안 보인다.
FATAL_PATTERNS = (
    "GR00T 롤아웃 컨텍스트 구성 실패",
    "정책 타입",
    "Traceback",
)

#: 시연 중 반드시 눈에 보여야 하는 경고. 로그에서 잡아 콘솔로 올린다.
WATCH_PATTERNS = (
    "loop under target",
    "관절 봉투 위반",
    "Packet drop",
    "홈 복귀",
    "이미 실행 중",
)


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def info(msg: str) -> None:
    print(c(f"  {msg}", "36"), flush=True)


def ok(msg: str) -> None:
    print(c(f"  ✅ {msg}", "32"), flush=True)


def warn(msg: str) -> None:
    print(c(f"  ⚠️  {msg}", "33"), flush=True)


def die(msg: str) -> None:
    print(c(f"\n  ❌ {msg}\n", "31"), flush=True)
    raise SystemExit(1)


# ────────────────────────────────────────────────── 0. 잔여 프로세스
def find_stale() -> list[tuple[int, str]]:
    """우리 서비스가 이미 떠 있는지 본다.

    `pkill -f` 로 정리하지 않는다 — 패턴이 **자기 셸의 명령 문자열에도** 들어 있어서
    호출한 쪽까지 죽는다(실제로 겪었다). PID 를 눈으로 확인하고 명시적으로 죽인다.
    """
    out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True).stdout
    mine = os.getpid()
    found = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_s, _, args = line.partition(" ")
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if pid in (mine, os.getppid()):
            continue
        # 파이썬이 `-m <우리 모듈>` 로 도는 것만 센다. bash -c 래퍼는 제외한다.
        if re.search(r"python[0-9.]*\s+.*-m\s+(orchestrator|robot_bridge|cloud_bridge|vlm_service)\.main",
                     args):
            found.append((pid, args))
    return found


# ────────────────────────────────────────────────── 프로세스 관리
class Service:
    def __init__(self, name: str, argv: list[str], *, env: dict | None = None) -> None:
        self.name = name
        self.argv = argv
        self.env = env
        self.log_path = LOG_DIR / f"{name}.log"
        self.proc: subprocess.Popen | None = None
        self._fh = None

    def start(self) -> None:
        self._fh = open(self.log_path, "w", encoding="utf-8")
        env = {**os.environ, **(self.env or {})}
        self.proc = subprocess.Popen(
            self.argv, stdout=self._fh, stderr=subprocess.STDOUT,
            cwd=str(REPO), env=env, start_new_session=True,
        )
        info(f"{self.name} 기동 (pid={self.proc.pid}) → {self.log_path}")

    def tail(self, n: int = 25) -> str:
        try:
            return "\n".join(self.log_path.read_text(encoding="utf-8",
                                                     errors="replace").splitlines()[-n:])
        except OSError:
            return ""

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def wait_for(self, needle: str, timeout: float, label: str = "") -> None:
        """로그에 `needle` 이 나올 때까지 기다린다. 죽거나 치명 로그가 뜨면 즉시 실패."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.tail(400)
            if needle in text:
                ok(f"{self.name} {label or needle}")
                return
            for bad in FATAL_PATTERNS:
                if bad in text:
                    print(self.tail(30))
                    die(f"{self.name} 기동 실패 — 로그에 '{bad}'. 위 내용을 볼 것.")
            if not self.alive():
                print(self.tail(30))
                die(f"{self.name} 이(가) 죽었다 (exit={self.proc.returncode}). 위 로그를 볼 것.")
            time.sleep(0.5)
        print(self.tail(20))
        die(f"{self.name} 준비 확인 실패 ({timeout:.0f}초 안에 '{needle}' 안 나옴)")

    def stop(self, grace: float = 8.0) -> None:
        if not self.alive():
            return
        info(f"{self.name} 종료 요청 (pid={self.proc.pid})")
        try:
            self.proc.terminate()
            self.proc.wait(timeout=grace)
            ok(f"{self.name} 정상 종료")
        except subprocess.TimeoutExpired:
            # robot_bridge 는 **유휴 상태에서 SIGTERM 을 못 받는다** — 정지 신호를
            # 활성 제어 루프 안에서만 폴링하기 때문이다. Damiao 모터는 마지막 명령을
            # 계속 붙들고 있어서 프로세스가 죽어도 팔에서 갑자기 힘이 빠지지는 않는다.
            warn(f"{self.name} 이 SIGTERM 에 응답하지 않는다 — SIGKILL")
            self.proc.kill()
            self.proc.wait(timeout=5)
        finally:
            if self._fh:
                self._fh.close()


# ────────────────────────────────────────────────── 기동 단계
def resolve_policy() -> str:
    snaps = sorted(glob.glob(POLICY_GLOB))
    if not snaps:
        die(f"정책 체크포인트를 못 찾았다: {POLICY_GLOB}\n"
            "     허브 캐시에 90epi 체크포인트가 받아져 있어야 한다.")
    return snaps[-1]


def check_real_prereqs() -> None:
    if not SCIEDU_PY.is_file():
        die(f"{SCIEDU_PY} 가 없다 — lerobot·GR00T 플러그인이 있는 인터프리터가 필요하다.")
    if not (VENDOR / "websockets").is_dir():
        die(f"{VENDOR}/websockets 가 없다.\n"
            f"     그 인터프리터에는 websockets 가 없어서 우리 쪽에 벤더링해야 한다:\n"
            f"       cd {REPO} && uv pip install --target vendor websockets==17.0.1")
    if not ROBOT_CONFIG.is_file():
        die(f"{ROBOT_CONFIG} 가 없다.")
    cfg = json.loads(ROBOT_CONFIG.read_text(encoding="utf-8"))
    path = cfg.get("config_path")
    if path and not pathlib.Path(path).is_file():
        die(f"robot_real.json 이 가리키는 설정이 없다: {path}")


def fetch_new_code() -> str | None:
    """참고용. 태블릿이 표시한 코드가 **정답**이라 이것만으로는 페어링되지 않는다."""
    try:
        req = urllib.request.Request(SESSION_API, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r).get("code")
    except Exception:
        return None


def ask_code() -> str:
    print()
    print(c("  ┌─ 태블릿 ───────────────────────────────────────────────┐", "35"))
    print(c("  │ 1) 태블릿에서 시연 페이지를 연다                        │", "35"))
    print(c("  │      https://openarm-special-web.neudive7.workers.dev/tablet", "35"))
    print(c("  │ 2) 시작 버튼을 눌러 **소리를 먼저 켠다**                │", "35"))
    print(c("  │      (오디오가 안 열리면 발화가 안 끝나 세션이 멈춘다)  │", "35"))
    print(c("  │ 3) 화면에 뜬 4자리 코드를 아래에 입력한다               │", "35"))
    print(c("  └────────────────────────────────────────────────────────┘", "35"))
    while True:
        code = input(c("\n  태블릿에 뜬 4자리 코드: ", "1")).strip()
        if re.fullmatch(r"\d{4}", code):
            return code
        print(c("  숫자 4자리로 입력한다.", "33"))


def monitor(services: list[Service]) -> None:
    """시연 중 중요한 로그만 콘솔로 끌어올린다."""
    print()
    print(c("  ── 감시 중 (Ctrl-C 로 전체 종료) ──", "1"))
    print(c(f"     로그: {LOG_DIR}/*.log", "2"))
    offsets = {s.name: 0 for s in services}
    while True:
        for s in services:
            if not s.alive():
                warn(f"{s.name} 이(가) 종료됐다 (exit={s.proc.returncode})")
                print(s.tail(15))
                offsets[s.name] = -1
                continue
            if offsets[s.name] < 0:
                continue
            try:
                text = s.log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            new, offsets[s.name] = text[offsets[s.name]:], len(text)
            for line in new.splitlines():
                if any(p in line for p in WATCH_PATTERNS):
                    warn(f"[{s.name}] {line.strip()[:160]}")
        time.sleep(1.0)


def main() -> None:
    ap = argparse.ArgumentParser(description="시연 런처 — 전 구간을 순서대로 띄운다")
    ap.add_argument("--sim", action="store_true",
                    help="실물 대신 시뮬레이터 백엔드 (하드웨어를 건드리지 않는다)")
    ap.add_argument("--no-vlm", action="store_true",
                    help="VLM 을 띄우지 않는다 (운영자 수동 판정 폴백으로 돈다)")
    ap.add_argument("--scenario", default="minsu_playdate_v1")
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    mode = "시뮬레이터" if args.sim else c("실물 로봇", "1;31")
    print()
    print(c("═══ OpenArm 특수교육 시연 런처 ═══", "1"))
    print(f"  모드: {mode}")

    # ── 0. 잔여 프로세스 ────────────────────────────────────────────────
    stale = find_stale()
    if stale:
        print()
        warn("이미 떠 있는 프로세스가 있다 — 그대로 두면 같은 CAN 버스를 둘이 잡는다:")
        for pid, cmdline in stale:
            print(f"       pid={pid}  {cmdline[:110]}")
        die("먼저 정리할 것:  kill -TERM " + " ".join(str(p) for p, _ in stale))
    ok("잔여 프로세스 없음")

    if not args.sim:
        check_real_prereqs()
        policy = resolve_policy()
        ok(f"정책 체크포인트 {pathlib.Path(policy).parent.parent.name[:40]}…")

    services: list[Service] = []

    def shutdown(*_):
        print()
        print(c("  ── 종료 ──", "1"))
        # 역순. robot_bridge 는 teardown(하드웨어 disconnect + 초기 자세 복귀)이 있어
        # 시간을 넉넉히 준다.
        for s in reversed(services):
            s.stop(grace=20.0 if s.name == "robot_bridge" else 8.0)
        print()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        # ── 1. orchestrator ────────────────────────────────────────────
        orch = Service("orchestrator", [
            str(VENV_PY), "-m", "orchestrator.main",
            "--scenario", args.scenario, "--log-dir", str(LOG_DIR / "session"),
        ])
        services.append(orch)
        orch.start()
        orch.wait_for("Application startup complete", 30, "준비 완료")

        # ── 2. robot_bridge ────────────────────────────────────────────
        if args.sim:
            rb = Service("robot_bridge", [
                str(VENV_PY), "-m", "robot_bridge.main", "--sim",
                "--url", f"{HUB}/ws/robot", "--frame-port", "8081",
            ])
        else:
            rb = Service("robot_bridge", [
                str(SCIEDU_PY), "-m", "robot_bridge.main", "--real",
                "--policy-path", policy, "--robot-config", str(ROBOT_CONFIG),
                "--url", f"{HUB}/ws/robot", "--frame-port", "8081",
            ], env={"PYTHONPATH": f"{REPO}:{VENDOR}"})
        services.append(rb)
        rb.start()
        if not args.sim:
            print()
            warn("실물 팔이 곧 홈으로 이동한다 (약 6초, STAND CLEAR 카운트다운).")
            warn("팔 주변에서 물러나 지켜볼 것.")
            print()
            # GR00T 로드는 조용히 실패할 수 있다 — 그러면 브리지는 살아 있는 것처럼
            # 보이는데 모든 명령이 데드라인까지 사라진다. 여기서 확실히 확인한다.
            rb.wait_for("GR00T 준비 완료", 600, "정책 로드 완료")
        rb.wait_for("오케스트레이터 연결됨", 60, "허브 연결")

        # ── 3. vlm_service ─────────────────────────────────────────────
        if not args.no_vlm:
            vlm = Service("vlm_service", [
                str(VENV_PY), "-m", "vlm_service.main", "--url", HUB,
                "--frames-url", "http://127.0.0.1:8081/frame/latest",
            ])
            services.append(vlm)
            vlm.start()
            vlm.wait_for("연결됨", 60, "허브 연결")
        else:
            warn("VLM 없이 돈다 — 판정은 운영자 콘솔에서 수동으로 낸다.")

        # ── 4~5. 태블릿 코드 → cloud_bridge ────────────────────────────
        code = ask_code()
        bridge = Service("cloud_bridge", [
            str(VENV_PY), "-m", "cloud_bridge.main",
            "--cloud-url", CLOUD, "--code", code, "--hub", HUB,
        ])
        services.append(bridge)
        bridge.start()
        bridge.wait_for("pair:established", 60, "태블릿과 페어링됨")

        print()
        ok("전 구간 준비 완료.")
        print()
        print(c("  세션 시작은 운영자 콘솔에서:", "1"))
        print(f"    {VENV_PY} tools/operator_cli.py")
        print(c("    (s = 세션 시작 · p = 일시정지 · a = 로봇 중단)", "2"))

        monitor(services)

    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(c(f"\n  ❌ {type(exc).__name__}: {exc}", "31"))
        for s in reversed(services):
            s.stop()
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
