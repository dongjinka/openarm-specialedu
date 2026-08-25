# 시연 배포 — 무엇을 어디에 올리고 어떻게 띄우는가

## 배치

전 프로세스가 **로봇이 물린 머신(leap)** 에 상주하고, 태블릿만 LAN 으로 붙는다.
로봇 턴이 46초(실행은 더 길 수 있음)라 그 사이 머신 간 홉이 없어야 하고,
카메라 프레임이 네트워크를 넘지 않아야 한다.

```
                     leap 머신 (로봇 · 카메라 3대 · GPU)
  ┌───────────────────────────────────────────────────────────┐
  │  우리 venv                        leap venv               │
  │  ├ orchestrator      :8000        └ robot_bridge  :8081   │
  │  ├ vlm_service                        (lerobot ·          │
  │  ├ voice_service (마이크)               openarm_sciedu)    │
  │  ├ 태블릿 서빙       :4173  (vite — Cloudflare 불필요)     │
  │  └ 운영자 CLI                                             │
  └───────────────────────────────────────────────────────────┘
                              ↑ LAN
                       안드로이드 태블릿
```

**venv 가 둘인 이유**: `robot_bridge` 만 `lerobot`·`openarm_sciedu` 가 필요하다.
나머지 셋은 순수 파이썬이라 우리 venv 로 충분하고, 그래야 **leap 의 환경을 흔들지 않는다.**

**시연 중 외부 의존은 둘뿐이다** — VLM 판정(OpenRouter)과 STT(CLOVA, 선택).
태블릿 서빙·상태 동기화·페어링은 전부 이 머신 안에서 끝난다. 인터넷이 끊기면 VLM 이
죽고 운영자 수동 판정으로 강등되지만, **세션 자체는 계속 돈다.**

---

## 0. 먼저 — 리포에 커밋이 없다

`openarm-specialedu` 는 **커밋이 하나도 없다.** `git clone` 으로 배포할 수 없다.
둘 중 하나를 먼저 한다.

```bash
# (a) 커밋해서 git 으로 옮긴다 — 이후 갱신이 쉽다
git add -A && git commit -m "..."
```

```bash
# (b) 커밋 없이 파일만 옮긴다 — 급할 때
rsync -av --exclude .venv --exclude logs --exclude .git \
      ~/2026-summer/openarm-specialedu/ leap:~/specialedu/
```

태블릿(`openarm-special-web`)도 복제본을 로컬에서 고친 상태다. 같은 방식으로 옮긴다.

---

## 1. API 키

키는 저장소 루트의 `.env` 한 곳에 둔다. 네 프로세스가 같은 파일을 읽으므로 매번
export 할 필요가 없고, 명령줄에 적어 셸 히스토리·`ps` 에 남기지 않아도 된다.

```bash
cp .env.example .env    # 그리고 값을 채운다
```

| 키 | 쓰는 곳 | 없으면 |
|---|---|---|
| `OPENAI_API_KEY` | VLM 물체 판정 | **판정이 전부 실패한다** |
| `CLOVA_SPEECH_SECRET` | STT 음성 인식 | 듣기만 꺼진 채 세션은 돈다 |
| `HUMELO_API_KEY` | TTS 발화 사전 생성 | 시연 중에는 필요 없다 (§2-b 에서만) |

`.env` 는 `.gitignore` 에 있다. 이미 설정된 환경변수는 덮지 않으므로,
한 값만 임시로 바꿔야 하면 `OPENAI_MODEL=... python -m ...` 이 그대로 이긴다.

> ⚠️ **VLM 을 OpenAI 로 바꾸면 판정 정확도를 다시 재야 한다.** 기록된 20/20 · 10/10 은
> gemini-3.7-flash 로 잰 값이고 프롬프트도 그 모델의 응답을 보며 다듬었다.
> `tools/eval_vlm.py --labels eval/labels_45.csv` 로 재측정한 뒤에 그 숫자를 쓴다.

## 2. 환경 진단 (설치는 하지 않는다)

```bash
# 우리 venv 쪽
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,voice]"
.venv/bin/python tools/deploy_check.py
```

```bash
# robot_bridge 쪽 — leap 의 venv 로 돌린다
/home/leap/Documents/openarmsciedu/.venv/bin/python tools/deploy_check.py --robot
```

`--robot` 이 `lerobot`·`torch`·`openarm_sciedu` 를 못 찾으면 **그 머신이 아니다.**
`websockets`·`fastapi`·`uvicorn`·`PIL` 이 없다면 그 venv 에 추가해야 하는데,
**그쪽 환경을 흔드는 일이므로 담당자 확인을 받는다.**

`groot_frozen_bf16` 등록 여부도 같이 찍는다 — 안 되면
[`GROOT_POLICY_DEPENDENCY.md`](GROOT_POLICY_DEPENDENCY.md).

---

## 3. 시연 전 실측 두 가지

**홈 포즈 봉투** — `right_gripper` 는 여유가 0.16° 뿐이다.

```bash
cd /home/leap/Documents/openarmsciedu && .venv/bin/python scripts/calibration/home_both.py --side right --hold 5
```

**벽시계 턴 시간** — `robot_deadline_ms: 69500` 은 데이터셋 시간에서 나온 **임시 하한**이다.
드라이런 1회로 실제 시간을 재고 그 값 × 1.3 으로 시나리오를 고친다.

---

## 3-b. 발화 wav 를 미리 만든다

고정 대본은 **실행 중에 합성하지 않는다** — 한 문장에 약 5초가 걸리고, 매 회차 같은
소리가 나야 하며, TTS 가 죽어도 세션이 돌아야 한다.

```bash
.venv/bin/python tools/render_lines.py \
    --profile ../openarm-sciedu/voice-pipeline/tts/config/humelo_nana.json
```

대사·파일명의 원본은 `scenarios/utterances_ko.json` 이다. 사람 녹음 10개는 건너뛰고
나머지 11개만 만든다. **목소리를 하나로 통일하려면** `--include-human` 을 준다 —
사람 녹음과 TTS 가 섞이면 아동에게는 화자가 둘로 들린다.

파일은 태블릿 정적 자산 폴더로 떨어지므로 빌드가 필요 없고 새로고침하면 바로 들린다.

## 4. 띄우는 순서

순서가 중요하다. 허브가 먼저 떠야 스포크가 붙고, 로봇이 프레임을 내야 VLM 이 본다.

```bash
# ① 허브
.venv/bin/python -m orchestrator.main
```

```bash
# ② 로봇 + 프레임 서버 — leap venv 로 돌린다
PYTHONPATH=~/specialedu /home/leap/Documents/openarmsciedu/.venv/bin/python -m robot_bridge.main \
    --real --policy-path leapshared/nuedive_test_60epi_new_20260824_182026_GR00T17 \
    --robot-config config/robot_demo.json --frame-port 8081
```

```bash
# ③ VLM — 판정 + 상시 감지
.venv/bin/python -m vlm_service.main --frames-url http://127.0.0.1:8081/frame/latest
```

```bash
# ④ 음성 인식 (선택 — 안 띄워도 세션은 완결된다)
.venv/bin/python -m voice_service.main
```

> **소리는 태블릿에서 난다.** 캐릭터 얼굴이 있는 곳에서 목소리가 나오는 편이 아동에게
> 자연스럽다. 대신 두 가지를 코드로 막아 두었다.
>
> **에코** — 촉진 발화는 `WAIT_CHILD` 에 머문 채 나가고 그 구간은 마이크가 열려 있다.
> 같은 방이라 로봇이 자기 목소리를 듣는다. 그래서 상태 머신이 **재생 중 마이크를 닫고**,
> 태블릿이 재생을 마치며 보내는 `advance` 로 **다시 연다.**
>
> **자동재생 정책** — 시연 절차 ⑥의 "화면 한 번 탭" 이 그 대응이다.
>
> 소리가 모터 소음(1~2kHz +13dB)에 묻히면 태블릿 볼륨을 올리거나, 스피커를 로봇 쪽으로
> 옮기는 선택지가 있다 — `Role.AUDIO` 스포크 자리를 계약에 남겨 두었다.

```bash
# ⑤ 태블릿 — 이 머신에서 서빙하고 안드로이드는 LAN 으로 붙는다
cd ../openarm-special-web && npm run serve
```

```bash
# ⑤ 태블릿을 LAN 에 붙이고 **화면을 한 번 탭한다**  ← 빼먹으면 세션이 멈춘다
```

태블릿에서 `http://<leap-lan-ip>:4173/tablet` 을 연다. 허브 주소는 페이지를 서빙한
호스트의 8000 포트를 기본으로 쓰므로 별도 설정이 없다. 다르면 `?hub=<ip>:8000`.

> **화면을 한 번 눌러야 한다.** "화면을 한 번 눌러 주세요" 버튼이 뜨면 **운영자가**
> 누른다 (아동이 아니다 — 설치 절차다).
>
> Chrome 은 사용자 제스처 없이 소리 있는 재생을 막는다. 태블릿은 뷰라서 아동이 누를
> 버튼이 없고 세션은 운영자가 열기 때문에, 이 탭이 유일한 제스처다. 빼먹으면 첫 발화가
> 막히고 → `onended` 가 안 뜨고 → `advance` 가 안 나가고 → **세션이 INTRO 에서 멈춘다.**
> 누르기 전에는 본 화면으로 넘어가지 않게 해 두어 건너뛸 수 없다.

### LAN 을 어떻게 붙이나

| 방법 | 언제 |
|---|---|
| **여행용 공유기** | 기본. leap 유선 · 태블릿 무선. 가장 예측 가능하다 |
| **leap 을 핫스팟으로** | 공유기가 없을 때. `nmcli device wifi hotspot ifname wlan0 ssid openarm-demo password ...` |
| **USB 테더링** | 현장 Wi-Fi 를 못 믿을 때. 태블릿이 라우터가 되고 leap 이 `192.168.42.x` 를 받는다. 케이블 하나로 끝난다 |

```bash
ip -4 addr show scope global | grep -oP 'inet \K[\d.]+'      # leap 의 주소
sudo ufw allow 4173/tcp && sudo ufw allow 8000/tcp             # 방화벽
```

> **AP 격리(client isolation) 를 미리 확인한다.** 현장·게스트 Wi-Fi 는 단말 간 통신을
> 막는 경우가 흔하고, 이 구성에서 **가장 흔한 실패 원인**이다. 둘 다 붙여놓고 태블릿
> 브라우저로 `http://<leap-ip>:4173` 이 열리는지 시연 전에 확인한다. 안 열리면
> 공유기나 USB 테더링으로 간다.
>
> HTTPS 는 필요 없다. 평문 HTTP + `ws://` 라 혼합 콘텐츠 문제가 없고, 태블릿은 마이크를
> 쓰지 않는다(마이크는 leap). 보안 컨텍스트가 필요한 기능이 하나도 없다.

> **`npm run preview` 를 쓰지 않는 이유.** 그건 `wrangler dev` — Cloudflare 도구다.
> 태블릿은 이제 Durable Object 를 쓰지 않고 허브에 직결하므로 필요가 없고, 시연 당일
> Cloudflare 로그인·네트워크라는 변수만 늘린다. `npm run serve`(= `vite dev`)로 같은
> 화면이 뜨는 것을 e2e 로 확인했다 — 그쪽이 5배 빠르기도 하다(54초 → 10.9초).
>
> `/operator` 브라우저 페이지는 아직 Durable Object 경로다. **시연에서는 쓰지 않는다** —
> 운영자 콘솔은 `tools/operator_cli.py` 다.

```bash
# ⑥ 운영자 콘솔 — 세션을 여는 것은 여기다. 태블릿에는 시작 버튼이 없다
.venv/bin/python tools/operator_cli.py
```

> **`advance` 는 태블릿이 낸다** — 재생이 끝난 시점을 아는 쪽이다. 태블릿이 없으면
> `--sim` 리허설용 타이머가 대신 돈다.

---

## 5. 로봇 없이 리허설

실물이 준비되기 전에도 전 구간이 돈다.

```bash
.venv/bin/python -m robot_bridge.main --sim --realistic --frames-dir /tmp/eval_set/thumbs
```

`--realistic` 은 실측 분포(평균 46.3초)로 돌려 **진짜 침묵 구간의 길이를 체감**하게 한다.
3항목이면 2분 18초다. 이 감각을 미리 겪어야 단계 표시가 왜 필요한지 알 수 있다.

---

## 6. 시연 중 손잡이

| 상황 | 조치 |
|---|---|
| 판정이 이상하다 | 운영자 CLI `o`/`x`/`d` 로 수동 판정 (VLM 을 덮어쓴다) |
| 로봇이 실패했다 | `ROBOT_FAIL` 에서 "로봇의 실수" 안내 후 손으로 넣고 `advance` |
| 아동이 과부하 | `p` 일시정지 → 팔 정지. `r` 재개는 턴을 **처음부터** 다시 |
| 팔이 위험하다 | `a` 중단 |
| 상시 감지가 오작동 | VLM 을 `--no-watch` 로 재기동 → 운영자 버튼만 |
| 소리가 안 난다 | 설치 탭을 빼먹었다. 새로고침 후 화면을 한 번 누른다 |
| 특정 발화만 무음 | 그 wav 가 없다. `tools/render_lines.py --only <id>` |
| 카메라 하나가 죽었다 | 새 턴이 자동으로 막힌다. 복구 후 자동 재개 |

---

## 7. 시연 후

세션 로그는 `logs/<session_id>.jsonl` 이다. Ran 문서 4절의 행동지표 14개가 여기서 나온다.

```bash
.venv/bin/python tools/scenario_check.py     # 흐름·지표 산출 여부 자가진단
```
