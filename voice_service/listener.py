"""에너지 기반 발화 감지 — **버튼이 없다.**

아동은 버튼을 누를 줄 모른다. 그래서 푸시투톡을 쓰지 않고, 말이 시작되면 녹음하고
조용해지면 멈춘다. 마이크를 언제 열지는 상태 머신이 정한다 (`SetListen`).

`openarm-sciedu/stt-eval/stt_eval/live.py` 의 구현을 옮긴 것이다. 두 가지를 그대로 지켰다:

1. **매 턴 소음 바닥을 다시 잰다.** 시작할 때 한 번만 재면, 팔이 멈춘 뒤로는 모든
   소리를 발화로 오인한다 (모터 소음 1~2kHz +13dB 실측).
2. **문턱 아래 프레임만 골라 바닥을 재지 않는다.** 그러면 문턱이 내려갈수록 더 조용한
   프레임만 집계돼 바닥이 따라 내려가고, 그게 다시 문턱을 끌어내리는 자기강화 루프가
   된다 — 턴을 거듭할수록 오탐이 폭증한다. 턴 시작 직후 **고정 구간**을 무조건
   주변 소음으로 본다.

`sounddevice` · `numpy` 를 인자로 받는다. 사운드카드 없는 곳에서도 테스트할 수 있게 하려는 것이다.
"""

from __future__ import annotations

import logging
import queue
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
CHANNELS = 1
FRAME = 320                      # 20ms

#: 소음 대비 이 배수를 넘으면 발화로 본다 (약 +16dB).
NOISE_MULTIPLE = 6.0
#: 바닥이 너무 조용할 때의 문턱 하한.
FLOOR_MIN = 180.0


@dataclass(frozen=True)
class Utterance:
    """감지된 발화 한 덩어리."""

    pcm16: bytes
    seconds: float
    floor: float
    peak: float

    @property
    def empty(self) -> bool:
        return self.seconds <= 0.0


def rms(block, np) -> float:
    return float(np.sqrt((block.astype("float64") ** 2).mean() + 1e-12))


def listen(
    sd, np, *,
    max_s: float = 12.0,
    silence_s: float = 0.9,
    min_speech_s: float = 0.3,
    ambient_s: float = 0.4,
    should_stop=None,
) -> Utterance:
    """말이 시작되면 녹음하고 조용해지면 멈춘다.

    `should_stop()` 이 True 를 주면 즉시 접는다 — 상태가 바뀌어 마이크를 닫아야 할 때다.
    """
    q: queue.Queue = queue.Queue()

    def cb(indata, _frames, _t, status):  # noqa: ANN001
        q.put(indata.copy())

    chunks: list = []
    ambient: list[float] = []
    peak = 0.0
    started = False
    silent_for = 0.0
    speech_s = 0.0
    t0 = time.monotonic()
    n_ambient = int(ambient_s * SAMPLE_RATE / FRAME)
    threshold: float | None = None

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                        dtype="int16", blocksize=FRAME, callback=cb):
        while True:
            if should_stop is not None and should_stop():
                break
            try:
                block = q.get(timeout=0.2).reshape(-1)
            except queue.Empty:
                if time.monotonic() - t0 > max_s:
                    break
                continue

            level = rms(block, np)

            # 고정 구간은 무조건 주변 소음이다. 조건부로 고르면 자기강화 루프가 된다.
            if len(ambient) < n_ambient:
                ambient.append(level)
                continue
            if threshold is None:
                floor = float(np.median(ambient))
                threshold = max(floor * NOISE_MULTIPLE, FLOOR_MIN)

            if level > threshold:
                started = True
                silent_for = 0.0
                speech_s += len(block) / SAMPLE_RATE
                peak = max(peak, level)
            elif started:
                silent_for += len(block) / SAMPLE_RATE

            if started:
                chunks.append(block)
                if silent_for >= silence_s and speech_s >= min_speech_s:
                    break
            if time.monotonic() - t0 > max_s:
                break

    floor = float(np.median(ambient)) if len(ambient) >= 5 else 0.0
    if not chunks or speech_s < min_speech_s:
        return Utterance(b"", 0.0, floor, peak)
    audio = np.concatenate(chunks)
    return Utterance(audio.tobytes(), len(audio) / SAMPLE_RATE, floor, peak)


def to_wav(pcm16: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """CLOVA 는 wav 를 받는다. 헤더만 붙인다."""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16)
    return buf.getvalue()
