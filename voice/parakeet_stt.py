"""parakeet_stt.py - NVIDIA Parakeet TDT 0.6B v3 as a Pipecat STT service.

Phase 3 of the Jarvis V2 upgrade (vault/research/jarvis-v2-upgrade-research.md).

Parakeet TDT 0.6B v3 beats whisper-large-v3 on English WER (6.32% vs 7.44%) and is
far faster. Here it runs on the **CPU** via onnx-asr (int8 ONNX, ~640MB) so it frees
the GPU entirely — a good trade on the 4GB RTX 4050 when the GPU is wanted elsewhere.
It is opt-in: set AI_CTO_STT_ENGINE=parakeet (default stays GPU whisper-turbo).

Drop-in for WhisperSTTService: same SegmentedSTTService contract (16-bit PCM in,
TranscriptionFrame out), so the rest of the pipeline is unchanged.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator

import numpy as np
from loguru import logger

from pipecat.frames.frames import ErrorFrame, Frame, TranscriptionFrame
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

MODEL_NAME = os.environ.get("AI_CTO_PARAKEET_MODEL", "nemo-parakeet-tdt-0.6b-v3")


class ParakeetSTTService(SegmentedSTTService):
    """Local Parakeet TDT transcription via onnx-asr (CPU, int8 ONNX)."""

    @property
    def wants_wav_segments(self) -> bool:
        """Receive raw 16-bit PCM, matching WhisperSTTService."""
        return False

    def __init__(self, *, quantization: str | None = "int8",
                 language: Language | None = Language.EN, **kwargs):
        super().__init__(**kwargs)
        self._language = language
        self._quantization = quantization
        self._model = None
        self._load()

    def can_generate_metrics(self) -> bool:
        return True

    def _load(self) -> None:
        import onnx_asr

        logger.debug(f"Loading Parakeet model {MODEL_NAME} (quant={self._quantization})...")
        # CPU provider on purpose: keeps the GPU free and avoids the
        # onnxruntime/onnxruntime-gpu conflict with the Kokoro ONNX runtime.
        self._model = onnx_asr.load_model(
            MODEL_NAME,
            quantization=self._quantization,
            providers=["CPUExecutionProvider"],
        )
        logger.debug("Loaded Parakeet model")

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        if not self._model:
            yield ErrorFrame("Parakeet model not available")
            return

        await self.start_processing_metrics()
        audio_float = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
        try:
            text = await asyncio.to_thread(self._model.recognize, audio_float, sample_rate=16000)
        except Exception as e:
            logger.error(f"Parakeet transcription failed: {e}")
            await self.stop_processing_metrics()
            yield ErrorFrame(f"Parakeet transcription failed: {e}")
            return
        await self.stop_processing_metrics()

        text = (text or "").strip()
        if text:
            logger.debug(f"Transcription: [{text}]")
            yield TranscriptionFrame(text, self._user_id, time_now_iso8601(), self._language)
