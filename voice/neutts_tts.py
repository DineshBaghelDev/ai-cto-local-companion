"""neutts_tts.py - Neuphonic NeuTTS Air/Nano as a Pipecat TTS service (Phase 2).

NeuTTS is an on-device speech LM with near-human quality and instant voice cloning
from a short reference clip. It runs on CPU, so it leaves the GPU free for whisper.
Opt in with AI_CTO_TTS_ENGINE=neutts (default stays Kokoro, which is lighter/faster).

Voice cloning: the "voice" is defined by a 3-15s reference wav + its transcript. By
default we ship a British-male reference generated from Kokoro (offline, works out of
the box). Drop in a real recording via AI_CTO_NEUTTS_REF_AUDIO / _REF_TEXT to clone any
voice — that is where NeuTTS genuinely beats Kokoro.

Backbone defaults to neutts-nano (0.5B) for CPU realtime; set AI_CTO_NEUTTS_BACKBONE to
neuphonic/neutts-air for higher quality at more latency.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import numpy as np
from loguru import logger

from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.tts_service import TTSService
from pipecat.transcriptions.language import Language
from pipecat.utils.audio.resamplers.resampler_factory import create_stream_resampler

VOICE_DIR = Path(__file__).resolve().parent
NEUTTS_DIR = VOICE_DIR / "models" / "neutts"
NEUTTS_SR = 24000  # NeuTTS native output sample rate

DEFAULT_BACKBONE = os.environ.get("AI_CTO_NEUTTS_BACKBONE", "neuphonic/neutts-nano")
DEFAULT_CODEC = os.environ.get("AI_CTO_NEUTTS_CODEC", "neuphonic/neucodec")
REF_AUDIO = Path(os.environ.get("AI_CTO_NEUTTS_REF_AUDIO", str(NEUTTS_DIR / "ref_default.wav")))
REF_TEXT_PATH = Path(os.environ.get("AI_CTO_NEUTTS_REF_TEXT", str(NEUTTS_DIR / "ref_default.txt")))


class NeuTTSService(TTSService):
    """Local NeuTTS synthesis with a cloned reference voice, streamed to the pipeline."""

    def __init__(self, *, backbone: str = DEFAULT_BACKBONE, codec: str = DEFAULT_CODEC,
                 ref_audio: Path = REF_AUDIO, ref_text_path: Path = REF_TEXT_PATH,
                 language: Language = Language.EN, **kwargs):
        super().__init__(push_start_frame=True, push_stop_frames=True, **kwargs)
        self._language = language
        self._resampler = create_stream_resampler()

        from neutts import NeuTTS

        logger.info(f"NeuTTS: loading backbone {backbone} + codec {codec} (CPU)...")
        self._tts = NeuTTS(backbone_repo=backbone, backbone_device="cpu",
                           codec_repo=codec, codec_device="cpu")
        if not ref_audio.exists():
            raise FileNotFoundError(
                f"NeuTTS reference audio not found: {ref_audio}. Run download_models.py "
                "or set AI_CTO_NEUTTS_REF_AUDIO to a 3-15s mono wav.")
        self._ref_text = ref_text_path.read_text(encoding="utf-8").strip() if ref_text_path.exists() else ""
        self._ref_codes = self._tts.encode_reference(str(ref_audio))
        logger.info(f"NeuTTS ready; cloned voice from {ref_audio.name}")

    def can_generate_metrics(self) -> bool:
        return True

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        logger.debug(f"{self}: Generating TTS [{text}]")
        try:
            await self.start_tts_usage_metrics(text)
            # The torch backend has no true streaming (infer_stream raises), so we
            # synthesize the whole utterance on a worker thread, then hand it to the
            # output transport in small frames for smooth, interruptible playback.
            wav = await self._synthesize(text)
            await self.stop_ttfb_metrics()
            frame_samples = int(NEUTTS_SR * 0.2)  # 200 ms frames
            for start in range(0, len(wav), frame_samples):
                chunk = wav[start:start + frame_samples]
                audio_int16 = (np.clip(chunk, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
                audio_data = await self._resampler.resample(audio_int16, NEUTTS_SR, self.sample_rate)
                yield TTSAudioRawFrame(
                    audio=audio_data, sample_rate=self.sample_rate,
                    num_channels=1, context_id=context_id,
                )
        except Exception as e:
            logger.error(f"NeuTTS synthesis failed: {e}")
            yield ErrorFrame(error=f"NeuTTS error: {e}")
        finally:
            await self.stop_ttfb_metrics()

    async def _synthesize(self, text: str) -> np.ndarray:
        import asyncio

        def _run() -> np.ndarray:
            wav = self._tts.infer(text, self._ref_codes, self._ref_text)
            return np.asarray(wav, dtype=np.float32).reshape(-1)

        return await asyncio.to_thread(_run)
