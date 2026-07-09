# AI CTO Local Companion

You are working on **AI CTO Local Companion**, a local-first Jarvis-style CTO assistant.

Goal: build a practical desktop CTO companion that can remember context, discuss architecture, create PRDs, dispatch Claude Code/Codex workers, monitor progress, escalate blockers, preview outputs, accept feedback, and optionally handle GitHub PRs only on explicit command.

## Hard Constraints

Zero paid APIs.

Allowed paid tools:

* Claude Code subscription
* Codex subscription

Forbidden:

* OpenAI API keys
* Anthropic API keys
* Deepgram
* ElevenLabs
* Cartesia
* Twilio
* Daily transport
* cloud LLM APIs
* auto-deploy
* auto-merge
* background cloud workers

## Product Direction

Build toward a local Jarvis-style CTO companion.

Core rules:

* Local-first.
* Human-readable Markdown memory.
* Voice through local mic/speaker.
* Coding through Claude Code/Codex CLI.
* Human approval before risky actions.
* No hidden automation.
* No paid API dependencies.

## Current Working Features

* Markdown vault in `vault/`, readable in Obsidian.
* Basic Memory local SQLite index.
* Pipecat desktop voice companion in `voice/`.
* PRD flow through `scripts/cto.py`.
* Coding runner through `scripts/coder.py`.
* AO adapter in `scripts/ao_runner.py`.
* Blocker escalation through `scripts/escalation.py`.
* Final-output preview saved to Markdown.
* CLI/voice feedback loop.
* Optional GitHub PR flow through `gh`.
* Runtime state in `~/.ai-cto/coder.db`.

Vault remains the human-readable source of truth.

## Startup

```powershell
cd "E:\Projects\AI CTO"
python scripts\cto.py doctor
.\scripts\start.ps1
```

Open:

```text
http://localhost:7860
```

Desktop voice starts in the background by default.

Use text/status only:

```powershell
.\scripts\start.ps1 -NoDesktopVoice
```

Clean restart:

```powershell
.\scripts\stop.ps1
.\scripts\start.ps1
```

Debug visible windows:

```powershell
.\scripts\start.ps1 -Visible
```

Logs:

```powershell
Get-Content -Tail 80 logs\voice.log
Get-Content -Tail 80 logs\watcher.log
```

List audio devices:

```powershell
cd voice
.\.venv\Scripts\python.exe desktop_voice.py --list-devices
```

Start AO for a repo:

```powershell
.\scripts\start.ps1 -Repo "C:\path\to\repo"
```

## Text Brain

Fallback order (browser text/status page, `/api/chat` only — not voice):

1. local answers first
2. `chatgpt-cli` if `CHATGPT_SESSION_TOKEN` is set
3. `tgpt` from `%USERPROFILE%\.local\bin\tgpt.exe`
4. `codex exec` in read-only mode

Never store tokens in chat, Markdown, logs, or repo files.

## Voice Brain

Groq (primary) + OpenRouter (same-session failover) drive the voice
conversation and its tools (memory, escalation, coding dispatch, web); Claude
Code / Codex remain the coding engines only. See
`vault/decisions/voice-brain.md`.

Put keys in a project-root `.env` (gitignored, auto-loaded by `start.ps1`):

```
GROQ_API_KEY=...          # required
OPENROUTER_API_KEY=...    # optional but recommended — voice goes silent on
                           # Groq outage/quota without it
FIRECRAWL_API_KEY=...     # optional — upgrades web_search/web_fetch
```

Other knobs: `AI_CTO_VOICE_BRAIN` (reserved), `AI_CTO_MEMORY_BRAIN=groq|haiku`
(auto-memory extraction brain, default `groq`), `AI_CTO_PROFILE=1` (per-turn
latency logging in `logs/desktop-voice.log`).

Never store tokens in chat, Markdown, logs, or repo files.

## Optional V2 Tools

Jarvis exposes optional LangGraph, browser, Gmail, and Calendar tools. They are
off by default and fail closed with a setup message when dependencies or OAuth
files are missing.

Browser automation:

```powershell
pip install playwright
python -m playwright install chromium
```

Gmail and Calendar:

```powershell
pip install google-api-python-client google-auth-oauthlib
```

Create a Google OAuth desktop client, save its client-secret JSON outside git
at `.run/google-client-secret.json`, then use `google_auth_status` or any
Gmail/Calendar tool to complete local OAuth. Tokens are written under
`AI_CTO_STATE` or `AI_CTO_GOOGLE_TOKEN` and are ignored by git.

LangGraph:

```powershell
pip install langgraph
```

Email sending and calendar event creation still require explicit approval.

## PRD to Runner Flow

```powershell
python scripts\cto.py prd --title "My Feature" --goal "What should change" --task "Implement the smallest useful change (agent: claude)"
python scripts\cto.py approve prds\my-feature.md
python scripts\cto.py dispatch --prd prds\my-feature.md --repo C:\path\to\repo
python scripts\cto.py poll
python scripts\cto.py collect
python scripts\cto.py preview --run <run-id> --cmd "python -m pytest -q"
python scripts\cto.py feedback --task <task-id> --message "Change X and rerun."
```

## GitHub Flow

Only use when `gh` is installed, logged in, and explicitly requested.

```powershell
python scripts\cto.py new-repo --path C:\path\to\new-repo --github owner/name --private
python scripts\cto.py pr --task <task-id>
python scripts\cto.py merge-pr --pr <number-or-url> --repo C:\path\to\repo --method squash
```

Never auto-create PRs, auto-merge, or auto-deploy.

## Reused Repos

* `basicmachines-co/basic-memory`: Markdown memory, MCP tools, SQLite index.
* Obsidian: vault UI.
* `pipecat-ai/pipecat`: local voice, VAD, turn detection, STT/TTS.
* `AgentWrapper/agent-orchestrator`: Claude Code/Codex workers in isolated worktrees.
* Claude Code CLI and Codex CLI: subscription-backed coding workers.

## Engineering Rules

* Keep changes minimal.
* Preserve existing CLI behavior.
* Do not add paid APIs or required cloud services.
* Do not store secrets.
* Prefer stdlib Python where practical.
* Keep Windows PowerShell support first-class.
* Keep Linux/macOS compatibility when reasonable.
* Add clear errors for missing tools.
* Update docs when commands or flows change.
* Use isolated worktrees for coding tasks.

## Safety Rules

Require explicit user command before destructive actions.

Destructive actions include deleting files, deleting branches, merging PRs, pushing remotes, changing secrets, editing shell profiles, or stopping unrelated processes.

Prefer previews, dry runs, clear logs, and reversible actions.

## MVP Definition

A user can run:

```powershell
.\scripts\start.ps1
```

Then type or speak a goal, create a PRD, approve it, dispatch Claude Code or Codex, monitor state, handle blockers, preview results, give feedback, create a PR, and merge only by explicit command.
