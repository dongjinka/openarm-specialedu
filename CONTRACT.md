# 이벤트 계약 v1 — OpenArm 특수교육 PoC

> 원본 설계 문서는 [`docs/CLAUDE_CODE_CONTEXT.md`](docs/CLAUDE_CODE_CONTEXT.md) 이고 **그대로 보존한다.**
> 이 문서는 원본 §4·§6 을 **실측 데이터셋·모델 메타데이터에 맞춰 교정한 판**이다.
> §4 의 아키텍처 원칙(허브-스포크, 책임 분리)은 하나도 바꾸지 않았다. 바뀐 것은
> **사실관계 3건과 상태 머신 보강**뿐이며, 각 변경에 근거를 붙였다.
>
> 기계 판독 가능한 원본은 [`orchestrator/events.py`](orchestrator/events.py) 다.
> 이 문서와 코드가 어긋나면 코드를 고친다.

---

## 0. 무엇이 왜 바뀌었나

실제로 확인한 값 (`meta/info.json`, `meta/tasks.parquet`, `meta/stats.json`,
모델 `config.json` / `train_config.json` / `policy_preprocessor.json`):

> ⚠️ **2026-08-25 갱신**: 정책을 `nuedive_test_60epi_new_20260824_182026` 으로 재학습하면서
> 아래 값이 **거의 전부 바뀌었다.** 옛 120epi 판의 숫자를 쓰면 조용히 잘못 동작한다.

| 항목 | 값 (60epi) | 옛 값 (120epi) |
|---|---|---|
| 에피소드 / 프레임 | **60** / 83,351 @ 30fps → **평균 46.3초** | 120 / 110,929 → 30.8초 |
| **학습 태스크 (유일)** | **`Open the backpack. Put things in the backpack. Close the backpack.`** | `Open a backpack. …` (관사가 달랐다) |
| 베이스 | `nvidia/GR00T-N1.7-3B` + VLM 인코더 `nvidia/Cosmos-Reason2-2B` | 동일 |
| 학습 범위 | `tune_llm: false` · `tune_visual: false` (projector·diffusion·vlln 만 학습) | 동일 |
| 액션 청크 | `chunk_size: 16` → 1회 추론 = **0.533초** 분량 | 동일 |
| 필수 입력 | 카메라 **3대 전부** + `observation.state[16]` + `task` 문자열 | 동일 |
| 정규화 | `normalize_min_max: true` + **`clip_outliers: true`** | 동일 |
| **관절 봉투** | 16개 **전부 교체**. `right_gripper` 최댓값이 음수(−17.3)가 됐고 `right_joint_7` 은 범위가 통째로 이동했다 | `robot_bridge/preflight.py` 참조 |

### 변경 1 — `robot_cmd` 1회 = 완결된 1 사이클

원본 §4.1 은 VLA 를 *"눈앞의 물건을 집어 넣는"* 짧은 프리미티브로, §12 는
*"프리미티브 1회 × N"* 으로 서술했다. 실제 1 에피소드는
**가방 열기 → 물건 1개 넣기 → 가방 닫기** 전체이고 **≈46.3초**다 (중앙 45.9 · 최대 60.5).

> **데드라인은 실측해야 한다.** 46.3초는 30fps 텔레옵 녹화의 *데이터셋 시간*이지 실행
> 시간이 아니다. `lerobot.rollout` 의 제어 루프가 30fps 를 못 따라가면(메모의 sync 14.4Hz)
> 같은 동작이 최대 2배까지 늘어난다. 시나리오의 `robot_deadline_ms: 69500` 은 **임시 하한**이고,
> 드라이런으로 벽시계 시간을 잰 뒤 그 값 × 1.3 으로 다시 잡는다.

→ `motion` 은 `open_place_close` 하나뿐이다. 물건마다 이 사이클을 반복한다.
   조기 종료는 정상 경로가 아니다(안전 중단에만 쓴다) — 닫기가 다음 턴의
   시작 상태를 만들기 때문이다.

### 변경 2 — 언어 조건은 "주입 금지" 가 아니라 "고정 문자열 필수"

GR00T 는 언어 입력을 **생략할 수 없다** (`language_key: "task"`,
`formalize_language: true`). 정확한 규칙:

```python
GROOT_TASK = "Open a backpack. Put things in a backpack. Close a backpack."   # 바이트 단위 동일
```

언어 인코더가 동결(`tune_llm: false`, `tune_top_llm_layers: 0`)돼 있고 태스크가 1개뿐이라,
다른 문자열은 **조향은 못 하면서** 프로젝터에 분포 밖 노이즈만 넣는다.
→ [`robot_bridge/preflight.py`](robot_bridge/preflight.py) 에 상수로 박혀 있다.

### 변경 3 — `robot_done` 종료 신호에서 그리퍼 휴리스틱 강등

1 사이클 안에서 그리퍼가 **여러 번** 열린다(가방 열기 / 물체 놓기 / 가방 닫기).
원본 §6 의 1순위 신호("그리퍼 열림 + N프레임 유지")는 첫 국면에서 오발화한다.
좌우 거동도 다르다. 60epi 실측:
`l_grip` [−61.9, **+4.7**] q50 **+2.5** / `r_grip` [−67.9, **−17.3**] q50 **−66.4**.

> ⚠️ **오른쪽 그리퍼가 위험 신호다.** 최댓값이 음수(−17.3)인데 중앙값은 −66.4 로 최솟값에
> 붙어 있다 — 이 데이터에서 오른쪽 그리퍼는 사실상 −17.3 위로 올라가지 않는다.
> 홈 포즈에서 그리퍼가 열려 있으면(0 근처) **봉투 밖이라 preflight 가 매 턴 추론을 막는다.**
> 시연 전에 홈 포즈의 `observation.state` 를 1회 실측해 확인해야 하고, 벗어나면
> 봉투가 아니라 홈 포즈를 맞춘다.

새 우선순위: ① 타임아웃 **69.5초**(46.3 × 1.5, 실측 후 갱신) ② 시작 자세 복귀 ③ **VLM 사후 확인**
④ 운영자 강제 종료. 그리고 `robot_done.success` 를 **필수 필드로 추가**했다 —
"넣기 성공"과 "타임아웃으로 떨어뜨림"을 구분하지 못하면 §7 평가 근거가 성립하지 않는다.

### 변경 4 — 상태 머신 보강

- **`BAG_SETUP` 삭제** — 가방은 운영자가 세션 전에 세팅한다. 아동 과제는
  "요청받은 물건을 책상에 올리기" 하나로 축소된다.
- **`ROBOT_PACK` → `ROBOT_TURN`** — 한 번의 호출이 '넣기'가 아니라 '열기+넣기+닫기'다.
- **`ROBOT_VERIFY` 신설** — 물체 삽입 **과 가방 닫힘**을 함께 확인해 `success` 를 정한다.
  닫기가 실패한 턴은 다음 턴의 시작 상태를 분포 밖으로 만든다.
- **`ROBOT_FAIL` 신설** — 원본에 `robot_error` 전이가 없었다.
  **로봇의 실패를 아동이 자기 실패로 받으면 안 된다** → `robot_mistake` 대본 발화.
- **`PAUSED` 신설** — 감각 과부하·휴식 요구 시 어디서나 진입/복귀.

---

## 1. 배선 — 허브-스포크

```
     tablet ──┐                        ┌── robot (robot_bridge)
              ├──  orchestrator  ──────┤
   operator ──┤   (유일한 제어 경유지)  └── capture
        vlm ──┘
```

**VLM → 로봇 경로는 존재하지 않는다.** 문서 규칙이 아니라 코드 구조로 강제된다 —
`ALLOWED_SENDERS` 가 역할별 허용 이벤트를 정의하고, 서버가 위반을 끊은 뒤
`forbidden_event` 로 기록한다 ([`orchestrator/events.py`](orchestrator/events.py),
[`orchestrator/main.py`](orchestrator/main.py)).

| 역할 | 보낼 수 있는 이벤트 |
|---|---|
| `operator` | `session_start` `child_placed` `judge_override` `verify_result` `force_state` `robot_abort` `pause` `resume` `bag_reset` `advance` |
| `vlm` | `judge` `verify_result` |
| `robot` | `robot_done` `robot_error` `progress_tick` `contact_anomaly` `camera_health` |
| `capture` | `child_placed` `camera_health` `zone_disturbed` |
| `tablet` | `advance` |

---

## 2. 상태 머신

```
IDLE → INTRO → SHOW_LIST → REQUEST → WAIT_CHILD → JUDGE
JUDGE --correct-->        CORRECT → ROBOT_TURN → ROBOT_VERIFY → NEXT
JUDGE --incorrect-->      INCORRECT → REQUEST          (로봇 팔 미동작)
JUDGE --already_packed--> DUPLICATE → REQUEST
ROBOT_TURN --error/camera_loss--> ROBOT_FAIL
ROBOT_VERIFY --fail-->    ROBOT_FAIL → (운영자 결정) → CORRECT 재시도 | NEXT
NEXT --남음--> REQUEST     NEXT --완료--> COMPLETE → END
PAUSED ←→ 어느 상태에서나 (운영자 전용, force_state 로는 못 들어감)
```

**불변식: 청크 스트리밍 중이 아니면 항상 홀드 컨트롤러.** 백드라이버블이라 방치하면 팔이 처진다.
`ROBOT_TURN` 이 홀드가 꺼지는 유일한 구간이다.

| 상태 | 발화 | 표정 | 비고 |
|---|---|---|---|
| `INTRO` | `intro_text_arrived` | happy | 문자 메시지 UI |
| `SHOW_LIST` | `show_list` | happy | 목록 3개 |
| `REQUEST` | 항목·프롬프트 레벨별 | waiting | 현재 항목 강조 |
| `WAIT_CHILD` | (무음) | waiting | 반응 시간 기준점 |
| `JUDGE` | (무음) | **thinking** | **즉시 표시**해 판정 지연의 공백을 메움 |
| `CORRECT` | `correct_generic` | happy | ✓ 는 **방금 담은 항목**에 붙는다 |
| `ROBOT_TURN` | **발화 금지** | neutral | ≈46.3초. `progress_tick` 으로만 진행 표시 |
| `ROBOT_VERIFY` | (무음) | thinking | 물체 삽입 + 가방 닫힘 |
| `INCORRECT` | `incorrect_generic` | thinking | **팔이 움직이지 않는다** |
| `DUPLICATE` | `duplicate_generic` | happy | |
| `ROBOT_FAIL` | `robot_mistake` | thinking | 아동의 실패가 아니다 |
| `COMPLETE` | `complete` | celebrating | |
| `PAUSED` | `pause_break` | waiting | 동작 중이었다면 팔을 멈춘다 |

**프롬프트 위계** — 오답마다 `verbal → hint → model` 로 올라가고, 상한을 넘어도
`model` 에 머무를 뿐 **실패로 끝나는 경로는 없다**.

---

## 3. 이벤트

공통 봉투: 로그 레코드에 `ts` · **단조 증가 `seq`** · `session_id` 가 붙는다.
벽시계만으로는 같은 밀리초 이벤트의 순서가 복원되지 않아 리플레이가 깨진다.

```jsonc
// VLM → Orchestrator — object 는 판정과 반드시 함께 (로깅·발화·검증에 필요)
{"type":"judge","object":"tree","should_pack":true,"confidence":0.94}

// Orchestrator → Robot — 정답일 때만 발행
{"type":"robot_cmd","cmd_id":"c17","motion":"open_place_close",
 "target":"tree","deadline_ms":69500}     // target 은 로깅·검증용. 정책 입력 아님

// Robot → Orchestrator
{"type":"robot_done","cmd_id":"c17","success":true,
 "reason":"verified|timeout|operator|aborted","duration_ms":46300}
{"type":"robot_error","cmd_id":"c17","reason":"grasp_failed|preflight|...","detail":"…"}
{"type":"progress_tick","cmd_id":"c17","elapsed_ms":8000}
{"type":"contact_anomaly","joint":"left_4","deviation":12.3}

// Orchestrator → Capture — 배치 구역 감시 모드. 상태 머신이 소유한다.
{"type":"set_watch","mode":"off|judge|guard"}
//   judge — 변화 → 정지 → VLM 1회 → 물체면 child_placed.  (WAIT_CHILD)
//   guard — **VLM 을 부르지 않고** 변화만 보고 운영자를 부른다.  (ROBOT_TURN)
//           이송 중 책상에 물건이 올라오면 정책이 본 적 없는 관측이 된다 —
//           60에피소드 이송 구간에 물건이 놓인 프레임은 하나도 없다.

// Capture → Orchestrator — guard 모드의 경고. 판정이 아니다.
{"type":"zone_disturbed","detail":"…"}

// Capture → Orchestrator — 3대 전부가 정책 필수 입력이다
{"type":"camera_health","cameras":{"follower_d455f":true,"left_wrist":false,
 "right_wrist":true},"all_ok":false}      // all_ok=false 면 새 턴을 시작하지 않는다

// VLM/Operator → Orchestrator — 로봇 턴 사후 확인.
// `bag_closed` 는 배치면과 **따로** 측정한다. 예전 구현은 "배치면이 비었다"는 신호
// 하나를 두 필드에 넣어, 가방이 열린 채 끝난 턴을 성공으로 기록했다.
{"type":"verify_result","cmd_id":"c17","object_in_bag":true,"bag_closed":true}

// Orchestrator → Tablet/Operator
{"type":"state","phase":"CORRECT","expression":"happy","utterance_id":"correct_tree",
 "target":"tree","prompt_level":"verbal","retry":0,
 "progress":{"packed":["flower"],"remaining":["whale"]}}

// Operator → Orchestrator
{"type":"judge_override","object":"whale","should_pack":false}
{"type":"force_state","phase":"REQUEST"}   // PAUSED 로는 들어갈 수 없다
{"type":"robot_abort","cmd_id":"c17"}  {"type":"pause"}  {"type":"resume"}  {"type":"bag_reset"}
```

- **`cmd_id` 필수** — 지연된 완료 신호가 다음 명령과 섞이는 것을 막는다.
  만료된 `cmd_id` 의 `robot_done` / `progress_tick` 은 기록만 하고 버린다.
- **`utterance_id`** 로 보내면 사전 녹음 재생. **VLM 생성 발화만** `utterance_text` 에 싣는다.
- **표정은 열거형** — `neutral|happy|thinking|celebrating|waiting`. 자유 생성 금지.

---

## 4. 안전 게이트 (코드에 박힌 것)

| 게이트 | 위치 | 동작 |
|---|---|---|
| 관절 봉투 검사 | `robot_bridge/preflight.py` | 학습 min/max 밖이면 **추론하지 않고** `robot_error` |
| 카메라 3대 | `machine.py` / `capture` | `all_ok=false` → 새 턴 차단, 동작 중이면 즉시 중단 |
| 역할 게이팅 | `events.ALLOWED_SENDERS` | VLM 이 로봇 이벤트를 보내면 거부 + 기록 |
| 세대 가드 | `machine.py` | 만료된 `cmd_id` 무시 |
| 일시정지 | `machine.py` | 동작 중 `pause` → 팔 정지. 재개는 턴을 **처음부터** 다시 |

**관절 봉투가 왜 중요한가** — 전처리기가 `normalize_min_max` + `clip_outliers: true` 로
동작한다. 실측값이 학습 범위 밖이면 **조용히 클리핑되어 정책이 잘못된 state 를 본다.**
에러 없이 엉뚱하게 움직인다. 봉투 값은 `meta/stats.json` 에서 생성했다(단위: 도).

---

## 5. 아직 열려 있는 것

- [ ] **`groot_frozen_bf16` 의 출처** — `openarmsciedu-vla`(LeRobot 포크)의
      `configuration_groot.py` 도 `"groot"` 로만 등록하고, `frozen_bf16` 문자열은 포크 전체에
      **0건**이다. 플러그인 리포에도 groot 정책이 없다. 그 머신의 로컬 체크아웃에만 있다는
      뜻이므로 배선 전에 `PreTrainedConfig.CHOICE_REGISTRY` 를 직접 확인해야 한다.
- [ ] `episode_object_map.csv` — 물체 종류 + **에피소드 시작 시 가방 상태**를 한 패스로 라벨링
- [ ] 배치 구역이 씬 카메라 **중앙 230×230 크롭 안**인지 홈 포즈에서 실제 캡처해 확인
- [ ] 지연 예산 실측 — `T_ack ≤ 1.5s` / `T_judge` 는 실측 중앙 3.6초·최대 14.1초라
      `judge_timeout_ms` 를 15초로 올렸다 / `T_robot` 은 벽시계 측정 대상
- [ ] **무발화 46초 × 3회(2분 18초 이상)** 의 상호작용 적정성 — 특수교육 자문 필요.
      실행 시간이 2배로 늘면 5분에 가까워진다. 단계 표시(open/place/close)가
      선택이 아니라 필수에 가까워졌다
- [ ] 3연속 성공률 실측 + **물체별·가방적재량별 분해**
