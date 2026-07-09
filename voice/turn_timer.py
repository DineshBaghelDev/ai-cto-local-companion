"""turn_timer.py - per-turn latency profiling (STT-done -> first token -> first audio).

Opt-in with AI_CTO_PROFILE=1. Three instances share one `TurnClock` and sit at
different points in the pipeline:

    [input, stt, timer.stt_tap(), pair.user(), llm, timer.llm_tap(),
     tts, timer.tts_tap(), output, pair.assistant()]

Each tap logs its own timestamp; the tts tap also logs the deltas for the whole
turn once first audio is seen, so a single log line gives the full breakdown
without needing to correlate across taps by hand.
"""

from __future__ import annotations

import os
import time

from loguru import logger

from pipecat.frames.frames import Frame, LLMTextFrame, TranscriptionFrame, TTSAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

PROFILE = os.environ.get("AI_CTO_PROFILE", "0") == "1"


class TurnClock:
    """Shared timing state for one conversation turn."""

    def __init__(self) -> None:
        self.stt_done: float | None = None
        self.first_token: float | None = None
        self.first_audio_logged = False

    def reset_turn(self) -> None:
        self.stt_done = time.monotonic()
        self.first_token = None
        self.first_audio_logged = False


class _Tap(FrameProcessor):
    def __init__(self, clock: TurnClock, stage: str, **kwargs):
        super().__init__(**kwargs)
        self._clock = clock
        self._stage = stage

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if not PROFILE:
            await self.push_frame(frame, direction)
            return

        if self._stage == "stt" and isinstance(frame, TranscriptionFrame):
            self._clock.reset_turn()
            logger.info(f"[profile] stt-done t=0.000s text={frame.text!r}")

        elif self._stage == "llm" and isinstance(frame, LLMTextFrame):
            if self._clock.stt_done is not None and self._clock.first_token is None:
                self._clock.first_token = time.monotonic()
                delta = self._clock.first_token - self._clock.stt_done
                logger.info(f"[profile] first-token +{delta:.3f}s")

        elif self._stage == "tts" and isinstance(frame, TTSAudioRawFrame):
            if (
                self._clock.stt_done is not None
                and not self._clock.first_audio_logged
            ):
                self._clock.first_audio_logged = True
                now = time.monotonic()
                total = now - self._clock.stt_done
                token_gap = (
                    now - self._clock.first_token if self._clock.first_token else None
                )
                gap_str = f", token-to-audio +{token_gap:.3f}s" if token_gap else ""
                logger.info(f"[profile] first-audio +{total:.3f}s{gap_str}")

        await self.push_frame(frame, direction)


def build_taps() -> tuple[FrameProcessor, FrameProcessor, FrameProcessor] | tuple[None, None, None]:
    """Return (stt_tap, llm_tap, tts_tap) sharing one clock, or (None, None, None) if disabled."""
    if not PROFILE:
        return None, None, None
    clock = TurnClock()
    return _Tap(clock, "stt"), _Tap(clock, "llm"), _Tap(clock, "tts")
