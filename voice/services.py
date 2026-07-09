"""services.py - shared, env-tunable STT/TTS/turn builders for bot.py and desktop_voice.py.

Phase 1 of the Jarvis V2 upgrade (vault/research/jarvis-v2-upgrade-research.md):
Whisper large-v3-turbo on the GPU, British-male Kokoro voice, and an optional
wake-phrase gate ("hey jarvis") for always-on desktop use.

Env knobs (all optional):
  AI_CTO_WHISPER_MODEL   Whisper model name (default: large-v3-turbo on GPU, small on CPU)
  AI_CTO_WHISPER_DEVICE  cuda | cpu             (default: cuda when ctranslate2 sees a GPU)
  AI_CTO_STT_LANGUAGE    STT language code      (default: en; empty = autodetect)
  AI_CTO_TTS_VOICE       Kokoro voice id        (default: bm_george)
  AI_CTO_WAKE_PHRASES    comma-separated wake phrases, or "off"  (default: hey jarvis, jarvis)
  AI_CTO_WAKE_TIMEOUT    seconds the mic stays awake after the phrase (default: 25)
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path

import numpy as np
from loguru import logger

from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.settings import assert_given
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregatorParams
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.whisper.stt import Model, WhisperSTTService
from pipecat.transcriptions.language import Language
from pipecat.turns.user_start.wake_phrase_user_turn_start_strategy import (
    WakePhraseUserTurnStartStrategy,
)
from pipecat.turns.user_turn_strategies import (
    UserTurnStrategies,
    default_user_turn_start_strategies,
)

VOICE_DIR = Path(__file__).resolve().parent
MODELS = VOICE_DIR / "models"

DEFAULT_VOICE = "bm_george"
DEFAULT_WAKE_PHRASES = "hey jarvis, jarvis"
DEFAULT_VAD_STOP_SECS = 1.2
DEFAULT_WAKE_TIMEOUT = 300
DEFAULT_TTS_SPEED = 1.25


class FastKokoroTTSService(KokoroTTSService):
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        logger.debug(f"{self}: Generating TTS [{text}]")
        try:
            await self.start_tts_usage_metrics(text)
            voice = assert_given(self._settings.voice)
            lang = assert_given(self._settings.language)
            if voice is None or lang is None:
                raise ValueError("Kokoro TTS voice and language must be specified")
            speed = float(os.environ.get("AI_CTO_TTS_SPEED", str(DEFAULT_TTS_SPEED)))
            stream = self._kokoro.create_stream(text, voice=voice, lang=lang, speed=speed)
            async for samples, sample_rate in stream:
                await self.stop_ttfb_metrics()
                audio_int16 = (samples * 32767).astype(np.int16).tobytes()
                audio_data = await self._resampler.resample(audio_int16, sample_rate, self.sample_rate)
                yield TTSAudioRawFrame(
                    audio=audio_data,
                    sample_rate=self.sample_rate,
                    num_channels=1,
                    context_id=context_id,
                )
        except Exception as e:
            yield ErrorFrame(error=f"Kokoro TTS error: {e}")
        finally:
            await self.stop_ttfb_metrics()


def _add_cuda_dll_dirs() -> None:
    """ctranslate2 on Windows can't find cuBLAS/cuDNN from the nvidia-*-cu12 pip
    wheels on its own; register their bin dirs with the DLL loader. It resolves
    them with a plain LoadLibrary (PATH search), so add_dll_directory alone is
    not enough — PATH must carry the dirs as well."""
    if os.name != "nt":
        return
    import sysconfig

    nvidia_dir = Path(sysconfig.get_paths()["purelib"]) / "nvidia"
    bin_dirs = [str(p) for p in nvidia_dir.glob("*/bin")]
    for bin_dir in bin_dirs:
        try:
            os.add_dll_directory(bin_dir)
        except OSError:
            pass
    if bin_dirs:
        os.environ["PATH"] = os.pathsep.join(bin_dirs) + os.pathsep + os.environ.get("PATH", "")


_add_cuda_dll_dirs()


# ---- STT --------------------------------------------------------------------------


def stt_device() -> str:
    dev = os.environ.get("AI_CTO_WHISPER_DEVICE", "").strip().lower()
    if dev in {"cuda", "cpu"}:
        return dev
    try:
        import ctranslate2

        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception as e:
        logger.warning(f"could not probe CUDA, using CPU STT: {e}")
        return "cpu"


def whisper_model(device: str) -> Model:
    # large-v3-turbo int8 needs ~1.5GB VRAM (fine on the 4GB RTX 4050) but is
    # far too slow for realtime on this CPU, hence the small fallback.
    default = "LARGE_V3_TURBO" if device == "cuda" else "SMALL"
    name = os.environ.get("AI_CTO_WHISPER_MODEL", "").strip().upper().replace("-", "_")
    return getattr(Model, name or default, getattr(Model, default))


def stt_language() -> Language | None:
    code = os.environ.get("AI_CTO_STT_LANGUAGE", "en").strip().lower()
    if not code:
        return None  # per-segment autodetect
    try:
        return Language(code)
    except ValueError:
        logger.warning(f"unknown AI_CTO_STT_LANGUAGE {code!r}, autodetecting")
        return None


def stt_engine() -> str:
    return os.environ.get("AI_CTO_STT_ENGINE", "whisper").strip().lower()


def stt_description() -> str:
    if stt_engine() == "parakeet":
        return "parakeet-tdt-0.6b-v3@cpu"
    device = stt_device()
    return f"{whisper_model(device).name.lower().replace('_', '-')}@{device}"


def build_stt():
    """STT service: GPU whisper-turbo by default, or Parakeet (CPU) when
    AI_CTO_STT_ENGINE=parakeet."""
    if stt_engine() == "parakeet":
        from parakeet_stt import ParakeetSTTService

        logger.info("STT: Parakeet TDT 0.6B v3 on CPU (int8 ONNX)")
        return ParakeetSTTService(language=stt_language())

    device = stt_device()
    model = whisper_model(device)
    compute = "int8_float16" if device == "cuda" else "default"
    language = stt_language()
    logger.info(f"STT: faster-whisper {model.name} on {device} ({compute}, lang={language})")
    return WhisperSTTService(
        settings=WhisperSTTService.Settings(model=model, language=language),
        device=device,
        compute_type=compute,
    )


# ---- TTS --------------------------------------------------------------------------


def tts_engine() -> str:
    return os.environ.get("AI_CTO_TTS_ENGINE", "kokoro").strip().lower()


def tts_description() -> str:
    if tts_engine() == "neutts":
        return f"neutts:{os.environ.get('AI_CTO_NEUTTS_BACKBONE', 'neuphonic/neutts-nano')}"
    return f"kokoro:{os.environ.get('AI_CTO_TTS_VOICE', DEFAULT_VOICE)}"


def build_tts():
    """TTS service: Kokoro by default (light/fast), or NeuTTS (cloned voice, more
    natural) when AI_CTO_TTS_ENGINE=neutts."""
    if tts_engine() == "neutts":
        from neutts_tts import NeuTTSService

        logger.info("TTS: NeuTTS (cloned voice, CPU)")
        return NeuTTSService()

    voice = os.environ.get("AI_CTO_TTS_VOICE", DEFAULT_VOICE).strip() or DEFAULT_VOICE
    logger.info(f"TTS: Kokoro voice {voice}")
    speed = float(os.environ.get("AI_CTO_TTS_SPEED", str(DEFAULT_TTS_SPEED)))
    logger.info(f"TTS speed: {speed:.2f}x")
    return FastKokoroTTSService(
        model_path=str(MODELS / "kokoro-v1.0.onnx"),
        voices_path=str(MODELS / "voices-v1.0.bin"),
        settings=KokoroTTSService.Settings(voice=voice),
    )


# ---- user turn params (VAD + optional wake phrase) --------------------------------


def wake_engine() -> str:
    """'transcript' (default, Smart-Turn wake phrase), 'openwakeword' (acoustic gate),
    or 'off'."""
    return os.environ.get("AI_CTO_WAKE_ENGINE", "transcript").strip().lower()


def wake_phrases() -> list[str]:
    raw = os.environ.get("AI_CTO_WAKE_PHRASES", DEFAULT_WAKE_PHRASES)
    if raw.strip().lower() in {"", "off", "none", "0"}:
        return []
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def build_wake_gate(on_wake=None):
    """Return an OpenWakeWordGate when AI_CTO_WAKE_ENGINE=openwakeword, else None.

    The gate goes right after transport.input() so STT never runs on ambient speech.
    """
    if wake_engine() != "openwakeword":
        return None
    from wake_gate import OpenWakeWordGate, ensure_models

    if not ensure_models():
        return None
    return OpenWakeWordGate(on_wake=on_wake)


def build_user_params(
    *,
    wake: bool,
    on_wake: Callable[[bool], Awaitable[None]] | None = None,
) -> LLMUserAggregatorParams:
    """Aggregator params: default Smart Turn stack, plus a wake-phrase gate if `wake`.

    `on_wake(awake)` is called on wake-phrase detection (True) and timeout (False)
    so callers can surface mic state in logs/UI.
    """
    strategies: UserTurnStrategies | None = None
    # The transcript wake phrase and the openWakeWord acoustic gate are mutually
    # exclusive — don't gate twice.
    phrases = wake_phrases() if (wake and wake_engine() == "transcript") else []
    if phrases:
        timeout = float(os.environ.get("AI_CTO_WAKE_TIMEOUT", str(DEFAULT_WAKE_TIMEOUT)))
        gate = WakePhraseUserTurnStartStrategy(phrases=phrases, timeout=timeout)

        @gate.event_handler("on_wake_phrase_detected")
        async def _on_detected(strategy, phrase):
            logger.info(f"wake phrase heard: {phrase!r}; listening for {timeout:.0f}s of activity")
            if on_wake:
                await on_wake(True)

        @gate.event_handler("on_wake_phrase_timeout")
        async def _on_timeout(strategy):
            logger.info("wake window closed; say the wake phrase to talk again")
            if on_wake:
                await on_wake(False)

        # Gate first so nothing reaches the normal start strategies until woken.
        strategies = UserTurnStrategies(start=[gate, *default_user_turn_start_strategies()])
        logger.info(f"wake phrases active: {phrases} (timeout {timeout:.0f}s)")

    vad_stop_secs = float(os.environ.get("AI_CTO_VAD_STOP_SECS", str(DEFAULT_VAD_STOP_SECS)))
    logger.info(f"VAD stop silence: {vad_stop_secs:.1f}s")

    return LLMUserAggregatorParams(
        vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=vad_stop_secs)),
        user_turn_strategies=strategies,
    )
