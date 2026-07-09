"""wake_gate.py - openWakeWord acoustic gate (Phase 2).

An always-on gate that drops microphone audio *before* it reaches STT until the wake
word ("hey jarvis") is detected acoustically. This is cheaper than the transcript-based
WakePhraseUserTurnStartStrategy because Whisper/Parakeet only runs once you've actually
addressed Jarvis, instead of transcribing every ambient sentence.

Opt in with AI_CTO_WAKE_ENGINE=openwakeword. Falls back to passthrough (never breaks
the pipeline) if openwakeword or its feature models are unavailable.

Placement: immediately after transport.input(), before STT.
"""

from __future__ import annotations

import os
import time

import numpy as np
from loguru import logger

from pipecat.frames.frames import Frame, InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

WAKE_MODEL = os.environ.get("AI_CTO_OWW_MODEL", "hey_jarvis")
THRESHOLD = float(os.environ.get("AI_CTO_OWW_THRESHOLD", "0.5"))
AWAKE_TIMEOUT = float(os.environ.get("AI_CTO_WAKE_TIMEOUT", "25"))
_CHUNK = 1280  # 80 ms at 16 kHz — openWakeWord's native frame size
_ACTIVITY_RMS = 500  # int16 RMS above this keeps the awake window open


def ensure_models() -> bool:
    """Make sure openWakeWord's shared feature models are present. Returns availability."""
    try:
        import openwakeword
        from openwakeword.utils import download_models

        download_models()  # no-op once cached
        return True
    except Exception as e:
        logger.warning(f"openWakeWord unavailable, wake gate will pass through: {e}")
        return False


class OpenWakeWordGate(FrameProcessor):
    """Drops input audio until the wake word is heard, then opens a listening window."""

    def __init__(self, *, on_wake=None, **kwargs):
        super().__init__(**kwargs)
        self._on_wake = on_wake
        self._awake_until = 0.0
        self._buf = np.empty(0, dtype=np.int16)
        self._model = None
        try:
            from openwakeword.model import Model

            self._model = Model(wakeword_models=[WAKE_MODEL], inference_framework="onnx")
            logger.info(f"openWakeWord gate active: {WAKE_MODEL!r} (threshold {THRESHOLD})")
        except Exception as e:
            logger.warning(f"openWakeWord model load failed, passing audio through: {e}")

    @property
    def _awake(self) -> bool:
        return time.monotonic() < self._awake_until

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if self._model is None or not isinstance(frame, InputAudioRawFrame):
            await self.push_frame(frame, direction)
            return

        samples = np.frombuffer(frame.audio, dtype=np.int16)
        if frame.num_channels > 1:
            samples = samples[:: frame.num_channels]

        already_awake = self._awake
        if already_awake and _rms(samples) > _ACTIVITY_RMS:
            self._awake_until = time.monotonic() + AWAKE_TIMEOUT  # extend on speech

        if self._detect(samples) and not already_awake:
            self._awake_until = time.monotonic() + AWAKE_TIMEOUT
            logger.info(f"wake word {WAKE_MODEL!r} detected; listening for {AWAKE_TIMEOUT:.0f}s")
            if self._on_wake:
                await self._on_wake(True)

        if self._awake:
            await self.push_frame(frame, direction)
        # else: swallow the audio frame (STT never sees ambient speech)

    def _detect(self, samples: np.ndarray) -> bool:
        self._buf = np.concatenate([self._buf, samples])
        fired = False
        while len(self._buf) >= _CHUNK:
            chunk, self._buf = self._buf[:_CHUNK], self._buf[_CHUNK:]
            scores = self._model.predict(chunk)
            if scores.get(WAKE_MODEL, 0.0) >= THRESHOLD:
                fired = True
                self._model.reset()  # avoid immediate re-trigger
                self._buf = np.empty(0, dtype=np.int16)
                break
        return fired


def _rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
