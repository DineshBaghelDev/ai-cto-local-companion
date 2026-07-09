"""Headless end-to-end test: acts as the browser.

Synthesizes a spoken question with Kokoro, streams it to the bot over a real
SmallWebRTC connection, records the bot's audio answer, and prints every UI event
(transcript, memory tool calls, bot text). Verifies the full loop:
  mic audio -> VAD -> Smart Turn -> whisper STT -> Claude(+memory MCP) -> Kokoro -> speaker audio
"""

import asyncio
import json
import os
import sys
import wave
from pathlib import Path

import aiohttp
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import AudioStreamTrack
from av import AudioFrame
from av.audio.resampler import AudioResampler
from kokoro_onnx import Kokoro

VOICE = Path(__file__).resolve().parent
QUESTION = "What did we decide about the voice stack for this project?"
SERVER = os.environ.get("AI_CTO_VOICE_SERVER", "http://127.0.0.1:7860")

SAMPLE_RATE = 48000
SAMPLES_PER_FRAME = 480  # 10 ms


class SpeechTrack(AudioStreamTrack):
    """Plays leading silence, the synthesized question, then trailing silence."""

    def __init__(self, pcm: np.ndarray):
        super().__init__()
        lead = np.zeros(SAMPLE_RATE // 2, dtype=np.int16)          # 0.5 s
        tail = np.zeros(SAMPLE_RATE * 30, dtype=np.int16)          # keep line open
        self._pcm = np.concatenate([lead, pcm, tail])
        self._pos = 0
        self._pts = 0

    async def recv(self):
        await asyncio.sleep(0.01)
        chunk = self._pcm[self._pos : self._pos + SAMPLES_PER_FRAME]
        self._pos += SAMPLES_PER_FRAME
        if len(chunk) < SAMPLES_PER_FRAME:
            chunk = np.zeros(SAMPLES_PER_FRAME, dtype=np.int16)
        frame = AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        self._pts += SAMPLES_PER_FRAME
        return frame


def synthesize_question() -> np.ndarray:
    k = Kokoro(str(VOICE / "models/kokoro-v1.0.onnx"), str(VOICE / "models/voices-v1.0.bin"))
    samples, sr = k.create(QUESTION, voice="am_adam", speed=1.0)
    # resample float32 [sr] -> int16 [48k]
    idx = np.linspace(0, len(samples) - 1, int(len(samples) * SAMPLE_RATE / sr))
    resampled = np.interp(idx, np.arange(len(samples)), samples)
    return (np.clip(resampled, -1, 1) * 32767).astype(np.int16)


async def watch_events(done: asyncio.Event, events: list):
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f"{SERVER}/ws/events") as ws:
            async for msg in ws:
                e = json.loads(msg.data)
                events.append(e)
                short = {k: (v[:120] if isinstance(v, str) else v) for k, v in e.items()}
                print(f"  [event] {short}", flush=True)
                if e.get("type") == "bot_text_end":
                    done.set()
                    return


async def main():
    print("synthesizing question audio...", flush=True)
    pcm = synthesize_question()
    print(f"question: {QUESTION!r} ({len(pcm)/SAMPLE_RATE:.1f}s of audio)", flush=True)

    received: list[np.ndarray] = []
    resampler = AudioResampler(format="s16", layout="mono", rate=24000)

    pc = RTCPeerConnection()
    pc.addTrack(SpeechTrack(pcm))

    @pc.on("track")
    def on_track(track):
        async def drain():
            while True:
                try:
                    frame = await track.recv()
                except Exception:
                    return
                for f in resampler.resample(frame):
                    received.append(f.to_ndarray().flatten().astype(np.int16))
        asyncio.ensure_future(drain())

    done = asyncio.Event()
    events: list[dict] = []
    watcher = asyncio.ensure_future(watch_events(done, events))

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{SERVER}/api/offer",
            json={"sdp": pc.localDescription.sdp, "type": pc.localDescription.type},
        ) as resp:
            answer = await resp.json()
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
    print("webrtc connected; streaming question...", flush=True)

    try:
        await asyncio.wait_for(done.wait(), timeout=240)
    except TimeoutError:
        print("TIMEOUT waiting for bot answer", flush=True)

    # TTS may start only after the text stream ends; wait until real (non-silent)
    # audio has arrived and the answer has had time to play out.
    for _ in range(30):
        await asyncio.sleep(2)
        audio_so_far = np.concatenate(received) if received else np.array([], dtype=np.int16)
        if len(audio_so_far) and float(np.abs(audio_so_far.astype(np.float32)).mean()) > 50:
            await asyncio.sleep(20)  # answer is flowing; let it finish
            break
    watcher.cancel()
    await pc.close()

    audio = np.concatenate(received) if received else np.array([], dtype=np.int16)
    voiced = float(np.abs(audio.astype(np.float32)).mean()) if len(audio) else 0.0
    out = VOICE / "test_answer.wav"
    if len(audio):
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
            w.writeframes(audio.tobytes())

    print("\n==== RESULT ====", flush=True)
    print(f"events: {len(events)}  "
          f"user_transcript: {any(e['type']=='user_transcript' for e in events)}  "
          f"memory_used: {any(e['type']=='memory' for e in events)}  "
          f"bot_text: {any(e['type']=='bot_text' for e in events)}", flush=True)
    print(f"answer audio: {len(audio)/24000:.1f}s, mean|amp|={voiced:.0f} -> {out if len(audio) else 'none'}",
          flush=True)
    ok = (any(e["type"] == "bot_text" for e in events) and len(audio) / 24000 > 1 and voiced > 50)
    print("E2E:", "PASS" if ok else "FAIL", flush=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
