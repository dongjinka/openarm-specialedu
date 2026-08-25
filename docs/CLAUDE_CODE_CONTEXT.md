# OpenArm 특수교육 PoC — 구현 컨텍스트

> Claude Code 세션 시작 시 이 파일을 먼저 읽힐 것. 설계 논의의 결론만 압축한 문서이며,
> 여기 적힌 **아키텍처 계약(§4)** 과 **안전 규칙(§10)** 은 임의로 바꾸지 말 것.

---

## 1. 프로젝트 개요

- 자폐 아동 대상 로봇 중재 시스템의 PoC. 데모까지 약 1주.
- 활동: 「친구 집에 놀러가기 위해 장난감 챙기기」
  - 태블릿에 친구(민수)의 문자 + 가져갈 장난감 목록 표시
  - 로봇이 물건을 하나씩 호명 → 아동이 지정 구역에 올림 → 판정 → 정답이면 로봇이 가방에 넣음
  - 전부 챙기면 마무리 피드백
- 로봇의 역할은 **작업 수행 기계가 아니라 상호작용 촉진자**. 물리 조작은 사회적 상호작용의 매개임.
- 타겟 기능(잠정): 실행기능 — 선택적 주의·방해자극 억제, 과제 개시/완수, 자기점검, 오류 수정
  - 특수교육 연구원/박사 자문으로 최종 확정 예정. 코드는 특정 구인에 종속되지 않게 둘 것.

## 2. 하드웨어 · 환경

- **로봇**: OpenArm 양팔 (`robot_type: bi_openarm_follower`), 7DOF×2, QDD 백드라이버블, 중력보상, ROS 2
- **카메라 3개**: `follower_d455f`(씬, RealSense D455), `left_wrist`, `right_wrist` — 각 480×640, 30fps
- **태블릿**: 로봇 머리에 장착. 표정·문자·목록·진행상태 표시
- **오디오**: 사전 녹음 음성 재생 (PoC에서 STT 없음)
- **GPU**: 40GB+ VRAM 확보됨 (GR00T 파인튜닝 가능)

## 3. 학습 모델 현황

- **정책**: NVIDIA Isaac GR00T (파운데이션 VLA), `new_embodiment` 로 양팔 OpenArm 등록
- **데이터셋**: `leapshared/neudive_test_120epi_0819_real_20260819_162506` (LeRobot v3)
  - 물체 3종(tree/flower/whale) 균등 혼합, `total_tasks: 1`, `task_index` 전부 0
  - **단일 태스크 = "집어서 가방에 넣기"**. 물체 종류는 시각으로 구분되며 정책 입력으로 라벨을 주지 않음
  - 가방이 이미 일부 차 있는 상태의 에피소드도 포함되어 수집됨
  - ⚠ HF 카드가 `total_episodes: 45` 로 보이는 이슈 있었음 → 학습 시작 로그에서 실제 에피소드 수 확인할 것
- 이전 검증: 1종·단발 5/5 성공 (어수선한 실사무실 환경)
- **에피소드→물체 매핑**: 메타에 없음. `episode_object_map.csv` 를 별도 관리 (도구는 `extract_episode_thumbnails.py` + `label.html`)

## 4. 아키텍처 계약 ★ 가장 중요

### 4.1 책임 분리 (절대 원칙)

```
VLM      : 지각 + 판정   — 책상 위 물체 식별, 체크리스트 대조
Orchestrator : 흐름 제어 — 상태 머신, VLA 호출 여부 결정, 로깅
VLA(GR00T)   : 실행만    — "호출되면 눈앞의 물건을 집어 넣는다"
```

- **VLA는 "넣을지 말지"를 모른다.** 오답일 때 안전한 이유는 정책이 거부해서가 아니라 **호출 자체가 없어서**임.
- **VLM은 VLA에게 직접 지시하지 않는다.** 모든 제어 흐름은 Orchestrator를 경유 (허브-스포크).
- 정책에 물체 라벨/언어 조건을 주입하지 말 것. 단일 태스크로 학습되어 있음.
- 시나리오(목록)가 바뀌어도 **재학습 불필요** — Orchestrator의 체크리스트만 교체.

### 4.2 컴포넌트

| 컴포넌트 | 역할 | 비고 |
|---|---|---|
| `capture_service` | 카메라 3대 독점 + 프레임 fan-out | 카메라 경합 방지. **필수** |
| `orchestrator` | 상태 머신, 이벤트 허브, 로깅 | FastAPI + WebSocket |
| `tablet_ui` | 아동용 화면 (표정·문자·목록·진행) | 브라우저, WS 구독 |
| `operator_console` | WoZ 판정/오버라이드/강제전이 | 브라우저, WS 구독. **데모까지 유지** |
| `robot_bridge` | GR00T 추론 서버 ↔ ROS 2 | `--sim` 모드 필수 |
| `vlm_service` | 물체 식별 + 판정 | 컨테이너 분리 |

### 4.3 프레임 배포 (제어 흐름과 분리)

- `capture_service` 가 카메라를 **독점**하고 배포:
  - → GR00T: ZMQ PUB/SUB `CONFLATE=1` 또는 공유메모리 (저지연, 최신 프레임만)
  - → VLM: HTTP `GET /frame/latest` (판정 시점에 1장)
- ROS 2 노드로 퍼블리시하고 양쪽이 구독하는 방식도 가능 (OpenArm이 ROS 2 네이티브)
- ⚠ RealSense는 보통 한 프로세스만 device를 잡음. 두 소비자가 직접 열면 통합 첫날 막힘.

### 4.4 이벤트 스키마

```jsonc
// VLM → Orchestrator
{"type":"judge","object":"tree","should_pack":true,"confidence":0.94}
// object 는 판정과 함께 반드시 같이 낼 것 (로깅·발화·검증에 필요)

// Operator → Orchestrator (오버라이드/강제)
{"type":"judge_override","object":"whale","should_pack":false}
{"type":"force_state","phase":"REQUEST"}
{"type":"robot_abort"}

// Orchestrator → Tablet
{"type":"state","phase":"CORRECT","expression":"happy",
 "utterance_id":"correct_tree","progress":{"packed":["flower"],"remaining":["tree","whale"]}}

// Orchestrator → Robot   (정답일 때만 발행)
{"type":"robot_cmd","motion":"pick_and_place","cmd_id":"c17"}

// Robot → Orchestrator
{"type":"robot_done","cmd_id":"c17","reason":"gripper_open|timeout|operator"}
{"type":"robot_error","cmd_id":"c17","detail":"..."}

// 안전/이상 로그
{"type":"contact_anomaly","joint":"left_4","deviation":12.3}
```

- `cmd_id` 필수 — 지연된 완료 신호가 다음 명령과 섞이는 것 방지
- `utterance_id` 로 보내면 사전 녹음 파일 재생. VLM 생성 발화만 텍스트 필드로 실을 것

### 4.5 상태 머신

```
IDLE → INTRO → SHOW_LIST → BAG_SETUP → REQUEST → WAIT_CHILD → JUDGE
JUDGE --correct--> CORRECT → ROBOT_PACK → (RETURN_HOME?) → NEXT
JUDGE --incorrect--> INCORRECT → REQUEST        (로봇 팔 미동작)
JUDGE --already_packed--> DUPLICATE → REQUEST
NEXT --남음--> REQUEST      NEXT --완료--> COMPLETE → END
```

| 상태 | 발화 | 태블릿 | 표정 |
|---|---|---|---|
| `INTRO` | "민수한테 문자가 왔어!" | 문자 메시지 UI | happy |
| `SHOW_LIST` | "같이 챙겨볼까?" | 목록 3개(그림+이름) | happy |
| `BAG_SETUP` | "가방 지퍼를 열고 내 앞에 놓아줄래?" | 가방 그림 + 화살표 | waiting |
| `REQUEST` | "꽃 장난감을 챙겨 보자! 책상 위에 올려줄래?" | 현재 항목 강조 + 배치 구역 표시 | waiting |
| `JUDGE` | (무음) | 유지 | thinking ← **즉시 표시**해 지연 공백 메움 |
| `CORRECT` | "잘했어!" | 항목 ✓ | happy |
| `ROBOT_PACK` | (무음, 동작음만) | 가방에 들어가는 애니메이션 | neutral |
| `INCORRECT` | "그건 민수가 말한 장난감이 아닌 것 같아…" | 현재 항목 재강조 | thinking |
| `DUPLICATE` | "그건 벌써 넣었어!" | 완료 항목 강조 | happy |
| `COMPLETE` | "좋아 이제 갈 준비 완료! 조심히 놀다와!" | 축하 + 완성된 가방 | celebrating |

- **`ROBOT_PACK` 중 발화 금지** — 아동 시선이 로봇 팔로 가야 함
- **프롬프트 위계** — 오답 2회 시 힌트(색/모양 단서), 3회 시 로봇이 가리키기.
  아동이 실패로 끝나는 경로가 없어야 함. `prompt_level` 을 상태에 유지하고 로깅할 것.

### 4.6 시나리오 JSON (코드에서 분리)

```jsonc
{
  "scenario_id": "minsu_playdate_v1",
  "friend_name": "민수",
  "checklist": ["flower", "whale", "tree"],   // 순서대로 호명
  "distractors": ["car"],                      // 아동 구역에만 배치, VLA 학습 대상 아님
  "objects": {
    "flower": {"ko":"꽃 장난감","image":"img/flower.png","utterances":{...}},
    "whale":  {"ko":"고래 장난감","image":"img/whale.png","utterances":{...}}
  },
  "prompt_hierarchy": ["verbal","hint","model"],
  "max_retries_per_item": 3
}
```

## 5. VLM 서비스 계약

```
입력: 씬 카메라 프레임 1장 + 체크리스트(주입) + 이미 담은 항목
출력(JSON only): {"object":"tree|flower|whale|other|none","should_pack":bool,"confidence":0.0-1.0}
```

- `confidence` 낮거나 `unclear`/`none` → **판정 보류**, 운영자 콘솔에 알림 후 사람이 확정
- 캡처 타이밍: 아동이 물건을 놓고 **물러난 뒤**. PoC에서는 운영자 버튼으로 트리거
- 발화 정책 (예측 가능성 = 치료적 기능):
  - **고정**: 정답 칭찬, 오답 안내, 시작/완료 대사 → 사전 녹음, VLM 생성 금지
  - **VLM 허용**: 자유 대화 응답, 격려 멘트 → 1~2문장, 아동 어휘, 주제 이탈 금지, 전량 로깅
  - **표정**: 자유 생성 금지. `neutral|happy|thinking|celebrating|waiting` 중 선택만

## 6. 로봇 브릿지

- GR00T 추론 서버는 **시작 시 1회 로드해 상주**. 물건마다 올렸다 내리지 말 것 (3B 로드에 수십 초)
- 호출 게이팅 = 추론 실행 제어이지 모델 로드 제어가 아님
- **`robot_done` 종료 신호** (정책은 완료를 알려주지 않음) — 하이브리드 권장:
  1. 그리퍼 열림 + N프레임 유지 감지
  2. 타임아웃 (학습 에피소드 평균 길이 × 1.5)
  3. 운영자 강제 종료 버튼
- **대기 중 홀드 컨트롤러 필수** — 백드라이버블이라 방치 시 팔이 처짐
- **`RETURN_HOME`**: 삽입 후 자세가 학습 에피소드 시작 자세 분포 안에 들어오면 생략 가능.
  검증 방법: `observation.state` 의 에피소드별 첫 프레임 분포 vs 삽입 후 실측치 비교.
  그리퍼 상태, 3회 누적 드리프트도 함께 확인할 것.
- **`--sim` 모드 필수**: 하드웨어 없이 `robot_cmd` 를 받아 지연 후 `robot_done` 만 반환.
  UI·오케스트레이터를 로봇 없이 병렬 개발하기 위함.

## 7. 로깅 (대표님 요구 "현재 기능 평가"의 근거 데이터)

에피소드(=아동 1회 활동)별 JSONL:

```jsonc
{"ts":"...","event":"state_change","phase":"REQUEST","target":"flower","trial":1}
{"ts":"...","event":"child_response","object":"whale","correct":false,
 "latency_ms":4200,"prompt_level":"verbal","retry":1}
{"ts":"...","event":"vlm_judge","object":"whale","should_pack":false,"confidence":0.91,
 "overridden":false}
{"ts":"...","event":"robot_cmd","cmd_id":"c17","motion":"pick_and_place"}
{"ts":"...","event":"robot_done","cmd_id":"c17","reason":"gripper_open","duration_ms":11800}
{"ts":"...","event":"contact_anomaly","joint":"left_4","deviation":12.3}
```

- 필수 필드: 정/오반응(T/F), 반응 시간, 재시도 횟수, 프롬프트 레벨, 독립 수행 여부
- 로봇 접촉 이상: 목표 관절값 vs 실제 관절값 편차(또는 토크) 임계 초과 시 기록

## 8. 저장소 구조 (제안)

```
openarm-poc/
├─ orchestrator/      # FastAPI + WS, 상태 머신, 로깅
│  ├─ states.py  events.py  scenario.py  logger.py  main.py
├─ capture_service/   # 카메라 독점 + fan-out
├─ vlm_service/       # 물체 식별·판정
├─ robot_bridge/      # GR00T 추론 ↔ ROS 2, --sim 지원
├─ tablet_ui/         # 아동 화면
├─ operator_console/  # WoZ 콘솔
├─ scenarios/         # *.json
├─ assets/            # 녹음 음성, 물체 그림, 표정
├─ tools/             # extract_episode_thumbnails.py, label.html, 평가 스크립트
└─ docker-compose.yml
```

## 9. 구현 순서 (권장)

1. 이벤트 계약 + 상태 머신 골격, `--sim` 로봇으로 end-to-end 루프
2. 태블릿 UI (표정·목록·진행) + 운영자 콘솔
3. 시나리오 JSON 로더 + 로깅
4. `capture_service` → VLM 서비스 연결 (운영자 폴백 유지)
5. 실제 GR00T 추론 연결, `robot_done` 신호 확정
6. 리허설: 폴백 경로(VLM 오판·정책 실패·팔 정지) 실제 연습

## 10. 안전 — 타협 불가

- **운영자 e-stop 상시 확보.** 자동 정지(아동 진입 감지 등)는 후순위여도 이건 전제조건
- 아동 대면 전 **빈 공간에서** 검증: 물체 3종 × 가방 상태(빈/1개/2개) 조합
- 로봇 동작 중 아동 손이 작업 영역에 없도록 물리적 배치 + "로봇 차례엔 손 내리기" 규칙
- 속도·토크 제한 낮게, 급격한 동작·큰 소음 회피 (감각 민감성)
- 방해자극이 **로봇 씬 카메라 화각 밖**인지 홈 포즈에서 실제 캡처해 확인
- 학습된 정책은 분포 밖(OOD)에서 예측 불가 동작 가능 → 검증 범위를 일반화 범위만큼 넓힐 것

## 11. 미해결 / 확인 필요

- [ ] 데이터셋 실제 에피소드 수 (45 vs 120) — 학습 로그에서 확인
- [ ] `episode_object_map.csv` 작성 (VLM 평가·물체별 정책 평가에 필요)
- [ ] `robot_done` 신호 방식 확정
- [ ] `RETURN_HOME` 생략 가능 여부 검증
- [ ] 3연속 성공률 실측 (단발 성공률의 3제곱이 됨. 80%→51%, 90%→73%)
- [ ] 총 지연 예산 실측 (아동 반응 → 로봇 반응까지 5초 넘기지 말 것)
- [ ] 방해자극 물체 확정 + 방해자극 사진 20~30장 (VLM `other` 분류 검증용)
- [ ] 타겟 구인 최종 확정 (연구원/박사 자문)

## 12. 하지 말 것 (안티패턴)

- ❌ VLM → VLA 직접 호출 경로 만들기
- ❌ 정책에 물체 라벨·언어 조건 주입 (단일 태스크로 학습됨)
- ❌ "여러 물건 넣기"를 정책 하나에 맡기기 → **프리미티브 1회 × N + 상태 머신 오케스트레이션**
- ❌ 물건마다 GR00T 모델 로드/언로드
- ❌ 운영자 수동 판정 버튼 제거 (데모까지 유지)
- ❌ 표정·핵심 발화를 VLM 자유 생성에 맡기기
- ❌ 카메라를 두 프로세스가 직접 열기
- ❌ 아동 대면에서 미검증 정책 실행

---

## 부록 A. 에피소드 라벨링 도구

```bash
pip install lerobot pillow numpy
python tools/extract_episode_thumbnails.py \
    --repo-id leapshared/neudive_test_120epi_0819_real_20260819_162506 \
    --out ./episode_labels
cp tools/label.html ./episode_labels/
cd episode_labels && python -m http.server 8000
# 브라우저에서 http://localhost:8000/label.html
# 키: 1=tree 2=flower 3=whale 0=지우기 ←→ 이동 → CSV 내려받기
```

## 부록 B. VLM 판별 평가 (매핑 CSV 확보 후)

- 각 에피소드 초반 프레임(팔이 물체를 가리기 전) + 방해자극 사진 20~30장
- 두 정확도를 **분리 측정**:
  1. 물체 인식 — 혼동행렬 (전체 정확도만 보면 약한 물체가 묻힘)
  2. 호출 판단 — 체크리스트 주입 시 `should_pack` 정확도
- 조건별 분해: 조명(커튼 개폐), 물체 위치(배치 구역 가장자리), 가방 상태(빈/일부 참)
- 목표: 물체별 95%+, 방해자극 `other` 분류가 특히 중요 (오분류 시 로봇이 엉뚱한 물건을 집음)
