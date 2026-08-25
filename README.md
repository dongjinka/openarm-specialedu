# OpenArm 특수교육 PoC

자폐 아동 대상 로봇 중재 시스템. 활동은 「친구 집에 놀러가기 위해 장난감 챙기기」 —
로봇이 물건을 하나씩 호명하고, 아동이 책상에 올리면, 판정 후 로봇이 가방에 넣는다.

로봇은 작업 수행 기계가 아니라 **상호작용 촉진자**다. 물리 조작은 사회적 상호작용의 매개다.

- **전체 구조: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** ← 프로세스·상태·실측 상수 조감도
- 설계 원본: [`docs/CLAUDE_CODE_CONTEXT.md`](docs/CLAUDE_CODE_CONTEXT.md) (보존)
- **이벤트 계약: [`CONTRACT.md`](CONTRACT.md)** ← 실측 데이터에 맞춰 교정한 판. 여기가 기준이다.

## 현재 상태

1단계(이벤트 계약 · 상태 머신 · `--sim` end-to-end)가 끝났고,
VLM 물체 판별을 실측해 **tree/flower/whale 97.8%** 를 확인했다.
물체를 다른 물체로 잘못 부른 경우는 0건이다 — 자세한 내용은 [eval/README.md](eval/README.md).

| 단계 | 상태 |
|---|---|
| 1. 이벤트 계약 + 상태 머신 + `--sim` end-to-end | ✅ |
| 2. 태블릿 UI + 운영자 콘솔 | ✅ 태블릿이 오케스트레이터 뷰로 전환됨 (아래 참조) |
| 3. 시나리오 로더 + 로깅 | 로더·로깅 동작. **녹음 9종 미확보** (프롬프트 위계·로봇 실수·일시정지) |
| 4. VLM 물체 판별 | ✅ 새 씬 재측정 **판정 20/20 · 사후확인 10/10** |
| 5. GR00T 실추론 | ✅ 배선 완료 (`lerobot.rollout` 래핑). **실기 미검증** |
| 6. 리허설 | 미착수 |

### 2026-08-25 — 60에피소드 재학습 반영

정책이 `nuedive_test_60epi_new_20260824_182026` 으로 바뀌면서 실측 상수가 거의 전부
교체됐다. 옛 값을 쓰면 **에러 없이 조용히 잘못 동작한다.**

| 항목 | 옛 값 | 새 값 |
|---|---|---|
| 태스크 문자열 | `Open a backpack. …` | `Open **the** backpack. …` |
| 평균 에피소드 | 30.8초 | **46.3초** (최대 60.5) |
| `robot_deadline_ms` | 46,000 | **69,500** (임시 하한 — 드라이런으로 재측정) |
| `judge_timeout_ms` | 5,000 | **15,000** (판정 실측 최대 14.1초) |
| 관절 봉투 | 120epi | **16개 전부 교체** |

`right_gripper` 는 최댓값이 음수(−17.3)이고 중앙값이 −66.4 다. **홈 포즈에서 그리퍼가
열려 있으면 preflight 가 매 턴 추론을 막는다** — 시연 전 홈 포즈 실측이 필수다.

## 실행

```bash
python3 -m venv --without-pip .venv
python3 -m pip --python .venv/bin/python install -e ".[dev]"
```

세 터미널로 나눠 띄운다.

```bash
.venv/bin/python -m orchestrator.main
```

```bash
.venv/bin/python -m robot_bridge.main --sim
```

```bash
.venv/bin/python tools/operator_cli.py
```

운영자 CLI 명령: `s` 시작 · `o` 정답 · `x` 오답 · `d` 중복 · `v`/`vf` 사후 확인 ·
`p`/`r` 일시정지·재개 · `a` 로봇 중단 · `q` 종료.
`--auto` 를 주면 전부 정답으로 자동 응답한다(스모크 테스트용).

리허설에서 진짜 침묵 구간의 길이를 체감하려면 실측 타이밍으로 돌린다.

```bash
.venv/bin/python -m robot_bridge.main --sim --realistic
```

## 테스트

```bash
.venv/bin/python -m pytest tests/ -v
```

- `test_machine.py` — 순수 상태 머신 전이 (서버·로봇 없이)
- `test_e2e_sim.py` — 진짜 서버 + 진짜 브릿지 코드, WS 로만 통신
- `test_logger.py` — §7 평가 근거 데이터의 봉투 무결성

## 구조

```
orchestrator/     events.py  states.py  machine.py  effects.py  hub.py  logger.py  scenario.py  main.py
robot_bridge/     backend.py  sim.py  groot_runner.py  preflight.py  main.py
vlm_service/      contract.py  prompts.py  service.py  frames.py  backends/  main.py
scenarios/        minsu_playdate_v1.json
eval/             labels_45.csv  results_*.json  README.md
tools/            operator_cli.py  extract_frames.py  contact_sheet.py  eval_vlm.py  label.html
```

VLM 을 붙여 돌리려면 네 번째 터미널을 띄운다. 로봇 브릿지가 `/frame/latest` 로
씬 카메라를 나눠 주므로 그 주소를 준다.

```bash
.venv/bin/python -m vlm_service.main --frames-url http://127.0.0.1:8081/frame/latest
```

로봇 없이 감시·판정 전 구간을 돌려보려면 `--sim` 브릿지에 이미지 디렉터리를 준다.

```bash
.venv/bin/python -m robot_bridge.main --sim --frames-dir /tmp/eval_set/thumbs
```

상시 감지를 끄고 운영자 버튼만 쓰려면 `--no-watch` 를 준다 (컷 라인 폴백).

**상태 머신은 I/O 를 하지 않는다.** `(SessionState, Event) -> (SessionState, [Effect])` 만
반환하고 시계조차 인자로 받는다. 그래서 서버 없이 전수 테스트할 수 있고, 로그를 그대로
리플레이할 수 있고, 운영자의 WoZ 오버라이드가 특수 경로가 아니라 그냥 또 하나의 이벤트가 된다.
부작용 실행(전송·기록·타이머)은 전부 `orchestrator/main.py` 몫이다.

## 하지 말 것

- ❌ VLM → 로봇 직접 호출 경로 만들기 (`ALLOWED_SENDERS` 가 막고 있다)
- ❌ 정책에 물체 라벨·언어 조건 주입 — `GROOT_TASK` 상수를 **바이트 단위로 그대로** 보낸다
- ❌ 물건마다 GR00T 모델 로드/언로드 — 시작 시 1회 로드해 상주
- ❌ 운영자 수동 판정 버튼 제거 — 데모까지 유지
- ❌ 표정·핵심 발화를 VLM 자유 생성에 맡기기
- ❌ 카메라를 두 프로세스가 직접 열기 — `robot_bridge` 만 열고 `/frame/latest` 로 나눈다
- ❌ 롤아웃 추론 루프 재구현 — `lerobot.rollout` 을 라이브러리로 감싼다
- ❌ `--inference.type=rtc` — 겹침보정이 그리퍼 릴리스를 삼킨다. sync 로 간다
- ❌ 옛 120epi 봉투·태스크 문자열 사용 — 조용히 클리핑되어 엉뚱하게 움직인다
- ❌ 태블릿에 세션 진행 로직 두기 — 태블릿은 `state` 를 그리고 `advance` 만 돌려주는 뷰다
- ❌ 아동 대면에서 미검증 정책 실행
