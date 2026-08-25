#!/usr/bin/env bash
# GPU 머신 조사 — 전부 read-only. 아무것도 만들거나 고치거나 지우지 않는다.
#  - python 은 -B 로 실행해 __pycache__ 바이트코드조차 남기지 않는다
#  - 설치·설정 변경·파일 쓰기 명령 없음
#
# 사용: bash probe_gpu_host.sh > probe.txt 2>&1   (그 뒤 probe.txt 내용을 전달)

echo "########## 1. 기본 ##########"
hostname; whoami; date; uname -a
echo
echo "########## 2. GPU ##########"
nvidia-smi --query-gpu=index,name,memory.total,memory.used --format=csv 2>&1
echo "--- 사용 중 프로세스 ---"
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv 2>&1
echo
echo "########## 3. python / lerobot 환경 후보 ##########"
echo "--- PATH 상 python ---"; which -a python python3 2>&1
echo "--- conda ---"; conda env list 2>/dev/null || echo "(conda 없음)"
echo "--- venv 후보 ---"
ls -d ~/*/.venv ~/.venv ~/*/*/.venv 2>/dev/null | head -20

probe_py() {   # $1 = python 실행 파일
  [ -x "$1" ] || return
  echo "===== $1 ====="
  "$1" -B -c "import sys; print('python', sys.version.split()[0])" 2>&1
  "$1" -B - <<'PYEOF' 2>&1
try:
    import lerobot, os
    print("lerobot", getattr(lerobot, "__version__", "?"))
    print("경로", os.path.dirname(lerobot.__file__))
except Exception as e:
    print("lerobot 없음:", type(e).__name__, e); raise SystemExit
try:
    from lerobot.configs.policies import PreTrainedConfig
    names = sorted(PreTrainedConfig.get_known_choices())
    print("등록된 policy type:", names)
    print(">>> groot_frozen_bf16 등록됨?", "groot_frozen_bf16" in names)   # ← 블로커의 답
except Exception as e:
    print("policy type 조회 실패:", type(e).__name__, e)
for m in ("torch", "transformers", "flash_attn", "decord", "torchcodec"):
    try:
        mod = __import__(m); print(f"  {m}", getattr(mod, "__version__", "?"))
    except Exception:
        print(f"  {m} 없음")
PYEOF
}

for p in $(which -a python3 python 2>/dev/null) $(ls -d ~/*/.venv/bin/python ~/.venv/bin/python 2>/dev/null); do
  probe_py "$p"
done

echo
echo "########## 4. lerobot 이 git 체크아웃이면 커밋 ##########"
for d in ~/lerobot ~/src/lerobot ~/repos/lerobot ~/work/lerobot; do
  [ -d "$d/.git" ] && { echo "--- $d ---"; git -C "$d" log -1 --format='%H %cd %s' 2>&1; git -C "$d" status --porcelain 2>&1 | head -5; }
done
pip show lerobot 2>/dev/null | head -8

echo
echo "########## 5. 체크포인트 / HF 캐시 ##########"
echo "--- 학습 출력 ---"
ls -la ~/outputs/train/groot_neudive_abs_20260819_232025/checkpoints/ 2>&1 | head
echo "--- HF 캐시 위치 ---"; echo "HF_HOME=${HF_HOME:-(미설정)}"
ls -d ~/.cache/huggingface/hub/models--leapshared--* 2>&1 | head
ls -d ~/.cache/huggingface/lerobot/leapshared/* 2>&1 | head
du -sh ~/.cache/huggingface 2>/dev/null

echo
echo "########## 6. ROS 2 / OpenArm ##########"
ls /opt/ros 2>&1; which ros2 2>&1
echo "ROS_DISTRO=${ROS_DISTRO:-(미설정)}"
ls -d ~/*openarm* ~/*/*openarm* 2>/dev/null | head

echo
echo "########## 7. 카메라 ##########"
ls /dev/video* 2>&1 | head
lsusb 2>/dev/null | grep -iE "realsense|intel|camera|webcam" | head
echo
echo "########## 완료 — 쓰기 작업 없음 ##########"
