# Voice Companion - fully-local Pipecat stack

Talks to the AI-CTO memory backend with zero paid APIs. The default voice path is:

```text
system mic -> Pipecat LocalAudioTransport -> Whisper STT -> Claude/Basic Memory brain -> Kokoro TTS -> system speaker
```

No Deepgram, ElevenLabs, Cartesia, Daily, Twilio, browser WebRTC, cloud STT, or
cloud TTS in the default path.

## Architecture

| Layer | Component |
|---|---|
| Transport | Pipecat `LocalAudioTransport` via PyAudio |
| VAD | Silero VAD |
| Turn detection | LocalSmartTurnAnalyzerV3 |
| STT | `WhisperSTTService`, model `base`, CPU |
| TTS | Kokoro ONNX in `voice/models/` |
| Brain | Claude Code subscription + basic-memory MCP |
| Text UI | `bot.py` + `static/index.html` |

## Setup

```powershell
cd "E:\Projects\AI CTO\voice"
py -3.13 -m venv .venv
.\.venv\Scripts\pip install "pipecat-ai[webrtc,local,silero,whisper,local-smart-turn-v3]" `
  kokoro-onnx claude-agent-sdk fastapi "uvicorn[standard]" requests
.\.venv\Scripts\python download_models.py
```

## Run

Normal app startup starts desktop voice and the browser text/status UI:

```powershell
cd "E:\Projects\AI CTO"
.\scripts\start.ps1
```

Open `http://localhost:7860` for text/status. Voice uses your system mic and
speaker directly; you should hear "Desktop voice is ready."

Text/status only:

```powershell
.\scripts\start.ps1 -NoDesktopVoice
```

List audio devices:

```powershell
cd "E:\Projects\AI CTO\voice"
.\.venv\Scripts\python desktop_voice.py --list-devices
```

Run desktop voice manually:

```powershell
cd "E:\Projects\AI CTO\voice"
.\.venv\Scripts\python desktop_voice.py
```

## Notes

- Browser WebRTC remains in `bot.py` only as a fallback/testing path.
- Desktop voice avoids ICE failures entirely.
- Fast text chat uses local answers first, then `chatgpt-cli` if
  `CHATGPT_SESSION_TOKEN` is set, then `tgpt`, then read-only Codex fallback.
