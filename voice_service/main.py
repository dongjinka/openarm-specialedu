"""Voice 스포크 — 아동의 말을 듣고 **의도만** 올린다.

    /ws/voice   set_listen  →  child_utterance

발화는 만들지 않는다. 응답은 전부 오케스트레이터가 고른 사전 녹음 대본이다.
자유 생성에 맡기면 아동이 문장을 학습할 수 없고, 로그의 `prompt_level` 이 무의미해져
독립 수행률을 계산할 수 없다 (§12).

**버튼이 없다.** 아동은 버튼을 누를 줄 모른다. 마이크 창은 상태 머신이 열고 닫으며
(`WAIT_CHILD` 에서만), 그 안에서 에너지 VAD 가 발화 경계를 스스로 찾는다.
그 덕에 에코 문제도 함께 풀린다 — 로봇이 말하는 동안과 팔이 도는 동안에는 아예 안 연다.

    python -m voice_service.main --url ws://127.0.0.1:8000
    python -m voice_service.main --provider null --fake-text "뭐라고?"   # 마이크 없이 배선 확인
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging

import websockets

import openarm_env

from voice_service.intents import classify
from voice_service.listener import listen, to_wav
from voice_service.transcribe import NullTranscriber, make_transcriber

logger = logging.getLogger(__name__)

#: 한 발화를 처리한 뒤 다음 창을 열기까지의 짧은 간격.
REARM_DELAY_S = 0.25


class VoiceService:
    def __init__(self, transcriber, *, fake_text: str | None = None) -> None:
        self.transcriber = transcriber
        self.fake_text = fake_text
        self.mode = "off"
        self._ws = None
        self._task: asyncio.Task | None = None
        self._audio = None          # (sounddevice, numpy) — 없으면 마이크 없이 돈다

    # ── 오디오 장치 ──────────────────────────────────────────────────────
    def _open_audio(self):
        if self._audio is not None:
            return self._audio
        try:
            import numpy as np
            import sounddevice as sd
        except Exception as exc:  # noqa: BLE001
            logger.warning("오디오 장치를 못 연다 (%s) — 듣기는 꺼진 채로 돈다", exc)
            self._audio = (None, None)
            return self._audio
        self._audio = (sd, np)
        return self._audio

    # ── 소켓 ─────────────────────────────────────────────────────────────
    async def _send(self, payload: dict) -> None:
        if self._ws is None:
            return
        await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def on_message(self, msg: dict) -> None:
        if msg.get("type") == "set_listen":
            self.set_mode(msg.get("mode", "off"))

    def set_mode(self, mode: str) -> None:
        if mode == self.mode:
            return
        logger.info("마이크 창 %s → %s", self.mode, mode)
        self.mode = mode
        if mode == "off":
            if self._task is not None:
                self._task.cancel()
                self._task = None
        elif self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    # ── 듣기 루프 ────────────────────────────────────────────────────────
    async def _loop(self) -> None:
        try:
            while self.mode == "open":
                text = await self._hear()
                if text:
                    decision = classify(text)
                    logger.info("들음 %r → %s (%.2f, %s)",
                                text, decision.intent.value, decision.confidence, decision.how)
                    await self._send({"type": "child_utterance", "text": text,
                                      "intent": decision.intent.value,
                                      "confidence": decision.confidence})
                await asyncio.sleep(REARM_DELAY_S)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("듣기 루프 실패 — 세션은 계속 돈다")

    async def _hear(self) -> str:
        if self.fake_text is not None:
            await asyncio.sleep(1.0)
            return self.fake_text

        sd, np = self._open_audio()
        if sd is None:
            await asyncio.sleep(1.0)
            return ""

        utterance = await asyncio.to_thread(
            listen, sd, np, should_stop=lambda: self.mode != "open"
        )
        if utterance.empty:
            return ""
        logger.info("발화 감지 %.1f초 (바닥 %.0f · 최대 %.0f)",
                    utterance.seconds, utterance.floor, utterance.peak)
        try:
            return await self.transcriber.transcribe(to_wav(utterance.pcm16))
        except Exception as exc:  # noqa: BLE001
            logger.warning("전사 실패: %s", exc)
            return ""

    async def run(self, base: str) -> None:
        url = f"{base}/ws/voice"
        async for ws in websockets.connect(url, ping_interval=20):
            self._ws = ws
            try:
                logger.info("연결됨 %s", url)
                async for raw in ws:
                    await self.on_message(json.loads(raw))
            except websockets.ConnectionClosed:
                logger.warning("연결 끊김 — 재연결")
                continue
            finally:
                self._ws = None
                self.set_mode("off")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://127.0.0.1:8000")
    p.add_argument("--provider", default=None, help="clova | null")
    p.add_argument("--fake-text", default=None,
                   help="마이크 없이 이 문장을 들은 것으로 친다 (배선 확인용)")
    args = p.parse_args()

    openarm_env.load()      # .env 의 API 키를 올린다
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    transcriber = NullTranscriber() if args.fake_text else make_transcriber(args.provider)
    svc = VoiceService(transcriber, fake_text=args.fake_text)
    logger.info("기동 (stt=%s)", transcriber.name)
    base = args.url.rstrip("/")
    if base.endswith("/ws/voice"):
        base = base[: -len("/ws/voice")]
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(svc.run(base))


if __name__ == "__main__":
    main()
