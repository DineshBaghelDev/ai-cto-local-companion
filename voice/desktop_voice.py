"""Desktop local voice runner: system mic -> Pipecat -> system speaker.

No browser, no WebRTC, no ICE. Uses the same Whisper/Kokoro/voice-brain stack as bot.py.

Always-on mode: a wake-phrase gate ("hey jarvis" / "jarvis" by default) holds the
mic open all day without triggering on ambient speech; after the phrase, the mic
stays live for AI_CTO_WAKE_TIMEOUT seconds of activity so follow-ups don't need
re-waking. Set AI_CTO_WAKE_PHRASES=off (or pass --no-wake) for push-to-talk-style
open mic.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import urllib.request

from loguru import logger

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

import services
import turn_timer
import voice_brain


async def log_event(event: dict) -> None:
    if event.get("type") == "user_transcript":
        logger.info(f"you: {event.get('text')}")
    elif event.get("type") == "bot_text":
        logger.info(f"cto: {event.get('text')}")
    elif event.get("type") in {"memory", "error", "failover"}:
        logger.info(event)
    await asyncio.to_thread(_post_event, event)


async def on_failover(reason: str) -> None:
    await log_event({"type": "failover", "text": f"Groq unavailable, switched to OpenRouter: {reason}"})


def _post_event(event: dict) -> None:
    data = json.dumps(event).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:7860/api/events",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=1).read()
    except Exception:
        pass


def list_devices() -> None:
    import pyaudio

    pa = pyaudio.PyAudio()
    try:
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            print(f"{i}: {d['name']} in={d['maxInputChannels']} out={d['maxOutputChannels']}")
    finally:
        pa.terminate()


def log_selected_devices(input_device: int | None, output_device: int | None) -> None:
    import pyaudio

    pa = pyaudio.PyAudio()
    try:
        in_info = pa.get_default_input_device_info() if input_device is None else pa.get_device_info_by_index(input_device)
        out_info = pa.get_default_output_device_info() if output_device is None else pa.get_device_info_by_index(output_device)
        logger.info(f"audio input: {in_info['index']} - {in_info['name']}")
        logger.info(f"audio output: {out_info['index']} - {out_info['name']}")
    finally:
        pa.terminate()


async def main_async(args: argparse.Namespace) -> None:
    log_selected_devices(args.input_device, args.output_device)
    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            input_device_index=args.input_device,
            output_device_index=args.output_device,
        )
    )
    stt = services.build_stt()
    tts = services.build_tts()
    llm = voice_brain.build_llm(on_failover=on_failover)
    tools = voice_brain.build_tools(emit_event=log_event)
    context = LLMContext(messages=[{"role": "system", "content": voice_brain.SYSTEM_PROMPT}], tools=tools)
    user_tap, assistant_tap, _obs_state = voice_brain.build_observer(log_event)

    async def on_wake(awake: bool) -> None:
        await log_event({"type": "wake", "awake": awake})

    wake_gate = None if args.no_wake else services.build_wake_gate(on_wake=on_wake)
    aggregators = LLMContextAggregatorPair(
        context,
        user_params=services.build_user_params(wake=not args.no_wake, on_wake=on_wake),
    )
    stt_tap, llm_tap, tts_tap = turn_timer.build_taps()
    stages = [transport.input()]
    if wake_gate is not None:
        stages.append(wake_gate)  # drop ambient audio before STT until "hey jarvis"
    stages.append(stt)
    if stt_tap is not None:
        stages.append(stt_tap)
    stages += [user_tap, aggregators.user(), llm, assistant_tap]
    if llm_tap is not None:
        stages.append(llm_tap)
    stages.append(tts)
    if tts_tap is not None:
        stages.append(tts_tap)
    stages += [transport.output(), aggregators.assistant()]
    task = PipelineTask(
        Pipeline(stages),
        params=PipelineParams(allow_interruptions=True),
        idle_timeout_secs=None,
    )

    if args.no_wake:
        logger.info("Desktop voice is ready. Speak after this line.")
    elif wake_gate is not None:
        logger.info("Desktop voice is ready. Say 'hey jarvis' to talk (acoustic gate).")
    else:
        phrases = services.wake_phrases()
        say = phrases[0] if phrases else "anything"
        logger.info(f"Desktop voice is ready. Say {say!r} to talk.")
    await PipelineRunner(handle_sigint=True).run(task)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--input-device", type=int)
    parser.add_argument("--output-device", type=int)
    parser.add_argument("--no-wake", action="store_true",
                        help="open mic: skip the wake-phrase gate")
    args = parser.parse_args()
    if args.list_devices:
        list_devices()
        return
    logger.info("Starting desktop voice. Press Ctrl+C to stop.")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
