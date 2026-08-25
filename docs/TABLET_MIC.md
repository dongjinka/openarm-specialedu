# 마이크를 태블릿에 둘 것인가

지금 설계는 마이크가 **leap 머신**에 있다(`voice_service`가 `sounddevice`로 직접 연다).
남은 미결 항목 중 하나가 "GPU 머신 오디오 장치 확인"인데, 태블릿에는 마이크가 확실히 있으니
그쪽으로 옮기면 그 불확실성이 사라진다. 그 방안을 검토했다.

## 결론

**시연 전에는 옮기지 않는다.** 단, leap 머신에 쓸 만한 입력 장치가 아예 없다면 이야기가 다르다.

결정은 10초짜리 확인 하나로 갈린다:

```bash
python3 -c "import sounddevice; print(sounddevice.query_devices())"
```

| 결과 | 할 일 |
|---|---|
| 입력 장치가 있다 | **그대로 둔다.** 아래 비용을 치를 이유가 없다 |
| 없다 | USB 마이크/헤드셋을 꽂는다. 아키텍처 변경 0 |
| 없고 USB 포트도 못 쓴다 | 그때 태블릿 마이크가 유일한 길이 된다 — 아래 설계대로 |

음성 층은 가산적이다. 하나도 안 와도 세션은 완결되므로, 이 결정이 시연을 막지는 않는다.

## 이득이라고 생각했다가 접은 것 두 가지

처음엔 태블릿 마이크가 명백히 낫다고 봤는데, 코드를 읽고 나니 근거 둘이 사라졌다.

**모터 소음.** `listener.py`는 모터 소음 1~2kHz +13dB 실측을 이유로 매 턴 소음 바닥을 다시 잰다.
태블릿은 아이 쪽, leap 머신은 팔 쪽이니 태블릿이 유리해 보였다. 그런데 상태 머신이
`_LISTEN_MODE = {WAIT_CHILD: "open"}` — **마이크는 `WAIT_CHILD`에서만 열린다.** 그때 팔은 멈춰 있다.
모터 소음은 이미 설계로 배제돼 있어서 마이크 위치와 무관하다.

**에코 제거.** 태블릿이 발화를 재생하고 태블릿이 들으면 같은 기기·같은 클럭이라 브라우저 AEC가
제대로 동작한다 — 맞는 말이지만, 지금도 에코 문제는 없다. 재프롬프트 중에는 마이크를 닫고
(`_reprompt`), 로봇이 말하는 동안에는 애초에 열지 않는다. AEC는 이미 닫힌 문에 자물쇠를 더 다는 격이다.

## 남는 이득

1. **불확실성 제거** — 태블릿에 마이크가 있는 것은 확실하다. leap 머신은 확인 전까지 모른다.
2. **입까지의 거리** — 태블릿은 아이 앞에 있다. SNR은 거리에 반비례하므로 이건 실질적이다.

## 비용

1. **LAN 폴백에서 마이크가 죽는다.** `getUserMedia`는 secure context를 요구한다. Cloudflare 배포는
   HTTPS라 괜찮지만, 터널이 실패해서 `http://<ip>:4173`로 떨어지면 **마이크만 통째로 사라진다**
   (`localhost`만 예외다). leap 머신 마이크는 두 경로 모두에서 동작한다.

2. **VAD 상수가 그대로 옮겨가지 않는다.** `NOISE_MULTIPLE = 6.0`, `FLOOR_MIN = 180.0`은 사운드카드
   원본 스트림 기준으로 잡은 값이다. 브라우저 오디오는 AEC/NS/AGC를 거친다. 특히 **AGC가 위험하다** —
   `listen()`은 시작 직후 고정 구간(`ambient_s = 0.4`)에서 바닥을 재고 그 뒤로는 문턱을 고정하는데,
   AGC는 그 뒤로도 게인을 계속 움직인다. 바닥을 잰 시점의 스케일이 5초 뒤에는 유효하지 않다.
   → `{echoCancellation: true, noiseSuppression: false, autoGainControl: false}`로 원본에 가깝게 받고,
   **실제 태블릿·실제 방에서 바닥을 다시 재야 한다.** 이건 코드가 아니라 측정 작업이다.

3. **시연 창에 신규 코드 ~120줄**, 그중 절반이 팀원 리포다. PR #2가 아직 리뷰 대기다.

## 만약 한다면 — 정확한 변경점

설계는 이미 이걸 받아들일 준비가 돼 있다. 예상보다 깔끔하다.

### 태블릿은 오디오만 올리고 의도는 올리지 않는다

`ALLOWED_SENDERS["child_utterance"] = frozenset({Role.VOICE, Role.OPERATOR})` — 태블릿은 여기 없다.
태블릿이 브라우저에서 STT까지 해버리면 이 게이트를 뚫어야 하는데, **뚫지 않는다.**
태블릿은 PCM 바이트만 나르고 VAD·STT·의도 분류는 전부 `voice_service`가 계속 소유한다.
안전 불변식이 그대로 유지된다.

### VAD는 한 줄도 안 고친다 — 이음매가 이미 있다

```python
def listen(sd, np, *, ...):   # listener.py — sd 를 인자로 받는다
```

docstring이 "사운드카드 없는 곳에서도 테스트할 수 있게 하려는 것"이라고 적어둔 주입 이음매인데,
태블릿 마이크를 붙이는 데 그대로 쓸 수 있다. `sd.InputStream(...)` 인터페이스만 흉내 내는 shim을
네트워크 큐에 물리면 된다. 자기강화 바닥 함정을 고친 알고리즘이 손대지 않은 채로 재사용된다.

```python
class NetworkAudio:
    """`sounddevice` 흉내 — 프레임을 네트워크 큐에서 꺼내 콜백에 밀어 넣는다."""
    def InputStream(self, *, samplerate, channels, dtype, blocksize, callback):
        return _Stream(self._q, self._np, blocksize, callback)   # __enter__/__exit__ + 펌프 스레드
```

### 변경 목록

| 곳 | 변경 | 크기 |
|---|---|---|
| `orchestrator/main.py` | `SetListen` 수신자에 `Role.TABLET` 추가 | 1줄 |
| `orchestrator/main.py` | `resync(TABLET)`에 `set_listen` 추가 | 1줄 |
| `orchestrator/main.py` | `receive_json()` → `receive()` 후 프레임 종류 분기. 바이너리는 **로그 없이** VOICE로 중계 | ~15줄 |
| `orchestrator/hub.py` | `send_audio()` — VOICE 에게만 `send_bytes` | ~8줄 |
| `voice_service/main.py` | `NetworkAudio` shim + 큐. `listen()`은 무수정 | ~40줄 |
| 웹 리포 | `AudioContext({sampleRate: 16000})` + AudioWorklet → Int16 → 바이너리 WS. `listen` 모드로 게이팅 | ~60줄 |

`receive_json()`을 `receive()`로 바꾸는 것이 유일하게 마음에 걸리는 부분이다 — 허브의 hot path이고,
`hub.py` docstring이 "배선이 곧 아키텍처다"라고 적어둔 자리다. 바이너리 프레임은 이벤트가 아니므로
`sender_allowed` 검사를 우회하는데, 우회해도 되는 이유(오디오는 이벤트가 아니다)를 주석으로 못 박아야 한다.

### 대역폭은 문제가 아니다

`listen_mode`가 `WAIT_CHILD`에서만 열리므로, 세션당 스트리밍 구간은 턴당 10~30초 × 3턴 = 30~90초다.
16kHz 모노 int16 = 32 KB/s → 세션당 **1~3 MB**. 터널로 보내도 무시할 수 있다.
브라우저에서 VAD를 돌릴 필요가 없다는 뜻이고, 그래서 검증된 파이썬 VAD를 그대로 쓸 수 있다.

## 검토했으나 버린 것

**브라우저 Web Speech API** (`webkitSpeechRecognition`). VAD+STT를 브라우저가 다 하고 비용이 0이다.
버린 이유: 태블릿이 텍스트를 생산하게 되어 역할 경계가 흐려지고, CLOVA 861ms 실측과 도메인 용어
보정 계층(`term_corrections.py`)을 통째로 버리게 된다. 아동 발화 품질도 미지수다.
