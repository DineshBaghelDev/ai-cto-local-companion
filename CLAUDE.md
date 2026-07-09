# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

AI CTO is a local companion/CTO MVP composed from existing tools plus thin glue.
It provides Markdown memory, text/voice discussion, PRD generation, coding-run
dispatch, isolated worker worktrees, review reports, blocker escalation, preview,
feedback, explicit PR creation, and explicit merge commands.

**Status (2026-07-09):** local MVP is running. Memory vault, browser text/status UI,
desktop voice, coding-run glue, blocker watcher, preview, feedback, repo creation,
and explicit PR/merge commands are built. Decision records live in
`vault/decisions/`; keep this file in sync with those decision notes.

## Hard Constraints

- No Deepgram, ElevenLabs, Cartesia, Twilio, Daily, or cloud STT/TTS. Auto-merge
  and auto-deploy stay out of scope.
- **Exception (user-authorized 2026-07-09):** cloud LLM API keys are allowed for
  the voice *conversation* brain and web search only — Groq (primary), OpenRouter
  (failover), and Firecrawl (optional web upgrade), all free-tier. See
  `vault/decisions/voice-brain.md`. Coding still runs exclusively on the Claude
  Code and Codex subscriptions, never a metered coding API.
- API keys are env-only (`.env`, gitignored) — never written into repo files,
  Markdown, generated `.cmd` files, or logs.
- The Markdown vault is the source of truth. Runtime SQLite/JSON is only task
  state and logs.
- Build glue code only. Reuse existing repos and CLIs wherever possible.
- Every important architecture choice belongs in `vault/decisions/`.

## Decided Architecture

| Layer | Choice | Notes |
|---|---|---|
| Memory | `basicmachines-co/basic-memory` | Basic Memory MCP over the Obsidian-readable Markdown vault, project `ai-cto`. |
| Visual vault | Obsidian | UI over the same Markdown files. |
| Text UI | `voice/bot.py` + `voice/static/index.html` | Browser page at `http://localhost:7860`; also serves `/api/chat`, `/api/blockers`, and logs memory/tool events. |
| Fast text brain | local first, then `chatgpt-cli`, then `tgpt`, then read-only Codex fallback | `CHATGPT_SESSION_TOKEN` enables `chatgpt-cli`; token must stay in env only, never files/logs. `tgpt` is fallback. Coding is not routed through this casual chat brain. |
| Voice | `pipecat-ai/pipecat` 1.5.0 | Default is `voice/desktop_voice.py`: Pipecat `LocalAudioTransport` via PyAudio system mic/speaker, Silero VAD, LocalSmartTurnAnalyzerV3, faster-whisper STT (`large-v3-turbo` on the RTX 4050 via CUDA int8; CPU `small` fallback), Kokoro ONNX TTS (`bm_george`), and an always-on "hey jarvis" wake gate. Shared builders + env knobs live in `voice/services.py`. Browser WebRTC remains fallback/testing only. See `vault/research/jarvis-v2-upgrade-research.md`. |
| Voice — swappable engines | env-selected in `voice/services.py` | **STT**: `AI_CTO_STT_ENGINE=whisper` (default, GPU) or `parakeet` (`voice/parakeet_stt.py`, NVIDIA Parakeet TDT 0.6B v3 on CPU via onnx-asr — frees the GPU). **TTS**: `AI_CTO_TTS_ENGINE=kokoro` (default, realtime) or `neutts` (`voice/neutts_tts.py`, NeuTTS voice cloning — **torch backend is ~RTF 5 on this CPU, not realtime**; realtime needs the GGUF/llama-cpp backend, which has no working Py3.13-Windows binary). **Wake**: `AI_CTO_WAKE_ENGINE=transcript` (default) or `openwakeword` (`voice/wake_gate.py`, acoustic "hey jarvis" gate that skips STT until addressed). |
| Voice — auto-memory | `voice/memory_observer.py` | After each spoken exchange, a background pass distills durable facts and writes them to the vault via Basic Memory. Default brain is **Groq** (free-tier, no Claude quota used); `AI_CTO_MEMORY_BRAIN=haiku` switches to a Claude Code Haiku session instead. Off with `AI_CTO_AUTO_MEMORY=0`. mem0 was evaluated and deferred (needs an OpenAI-compatible extractor). |
| Voice — actions | `voice/actions.py` | `quick_task(target, instruction)` edits any file/folder under the E: drive by spawning a headless `claude -p --permission-mode acceptEdits` worker (voice brain itself stays read-only; edits are scoped to allowed roots only). `open_project(name, agent)` opens a project in an interactive Claude/Codex Windows Terminal tab. Folder resolution uses Everything CLI (`es.exe`) when present, else a bounded filesystem walk. Exposed to the brain as pipecat function-call tools (`voice/voice_brain.py`). |
| Voice brain | Groq primary, OpenRouter same-session failover | `voice/voice_brain.py`: `FailoverLLMService` (OpenAI-compatible, native streaming + native tool calling) drives conversation and the same tool set the old MCP brain had (memory, escalation, coding dispatch, web) as plain `FunctionSchema` tools calling `scripts/{escalation,coder,jarvis}.py` + `voice/actions.py` directly — no MCP. Claude Code / Codex are dispatched *by* those tools for actual coding work, but no longer host the conversation itself. Requires `GROQ_API_KEY`; `OPENROUTER_API_KEY` optional but strongly recommended (voice goes silent on Groq outage/quota without it). See `vault/decisions/voice-brain.md`. |
| Voice — web | `scripts/jarvis.py` | `web_search`/`web_fetch` use Firecrawl (clean markdown/JS-page extraction) when `FIRECRAWL_API_KEY` is set, else DuckDuckGo HTML + urllib. Falls back automatically on any Firecrawl error. |
| Voice — profiling | `voice/turn_timer.py` | `AI_CTO_PROFILE=1` logs STT-done -> first-token -> first-audio deltas per turn, for diagnosing latency. |
| Coding runner | AO adapter + `scripts/coder.py` | Uses Claude Code and Codex workers in isolated git worktrees. See `vault/decisions/coding-runner.md` and `vault/decisions/agent-orchestrator-repo.md`. |
| Escalation | desktop notification + local voice/text decision | No phone/Twilio path. Human decisions are saved to Markdown and task logs before resume. |
| GitHub | `gh` explicit commands | PR creation and merge are explicit user actions only. No auto-merge. |

## Common Commands

```powershell
cd "E:\Projects\AI CTO"
python scripts\cto.py doctor
.\scripts\start.ps1
.\scripts\stop.ps1
```

Start text/status without desktop voice:

```powershell
.\scripts\start.ps1 -NoDesktopVoice
```

Pin desktop voice devices:

```powershell
.\scripts\start.ps1 -VoiceInputDevice 4 -VoiceOutputDevice 6
```

List audio devices:

```powershell
cd voice
.\.venv\Scripts\python.exe desktop_voice.py --list-devices
```

Use ChatGPT Plus session-token CLI for fast text chat:

```powershell
$env:CHATGPT_SESSION_TOKEN="paste_token_here"
.\scripts\start.ps1
```

Voice brain keys (Groq required, OpenRouter/Firecrawl optional) go in a
project-root `.env` (gitignored, loaded automatically by `start.ps1`):

```
GROQ_API_KEY=...
OPENROUTER_API_KEY=...
FIRECRAWL_API_KEY=...
```

Do not write tokens into repo files, Markdown, generated `.cmd` files, or logs.
If a token was pasted into chat, tell the user to revoke/rotate it.

## Coding Flow

```powershell
python scripts\cto.py prd --title "My Feature" --goal "What should change" --task "Implement the smallest useful change (agent: claude)"
python scripts\cto.py approve prds\my-feature.md
python scripts\cto.py dispatch --prd prds\my-feature.md --repo C:\path\to\repo
python scripts\cto.py poll
python scripts\cto.py collect
python scripts\cto.py preview --run <run-id> --cmd "python -m pytest -q"
python scripts\cto.py feedback --task <task-id> --message "Change X and rerun."
```

GitHub commands, only when `gh` is installed and logged in:

```powershell
python scripts\cto.py new-repo --path C:\path\to\new-repo --github owner/name --private
python scripts\cto.py pr --task <task-id>
python scripts\cto.py merge-pr --pr <number-or-url> --repo C:\path\to\repo --method squash
```

## Explicitly Rejected Or Deferred

- Graphiti: future work only; needs graph DB and per-ingest LLM calls.
- Custom vector DB: rejected for V1.
- Custom worktree engine: rejected unless the chosen runner cannot support the flow.
- Custom voice stack: rejected while Pipecat local audio works.
- Daily/Twilio/cloud voice transport: rejected by remaining zero-paid-API-transport constraint (the LLM exception above doesn't extend to audio transport/STT/TTS).
- Windows-native STT (WinRT `Windows.Media.SpeechRecognition`): deferred — it wants to own the mic, which conflicts with Pipecat's `LocalAudioTransport`. `faster-whisper` GPU stays default; prototype as an isolated spike later.
- Auto-merge and auto-deploy: rejected for V1.

## Environment Gotchas

- Host is Windows 11. AO runs under WSL2 when used; Pipecat and Basic Memory run
  on native Windows.
- The repo lives on `E:\Projects\AI CTO`.
- Voice venv is `voice\.venv`.
- Whisper/Kokoro models download once, then run locally. Kokoro files live in
  `voice\models`; Whisper uses the local Hugging Face cache.
- This E: drive had hardware trouble on 2026-07-07 and may still throw disk
  errors. Keep backups current; if writes fail with "device is not ready", stop
  and back up immediately.
- Use encoding-safe edits. Older PowerShell `Get-Content`/`Set-Content` defaults
  have already caused mojibake in Markdown.
