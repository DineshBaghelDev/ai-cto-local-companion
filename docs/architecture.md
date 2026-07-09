# AI CTO MVP Architecture

Source of truth for durable memory is the Markdown vault at `vault/`. Source of truth for architecture choices is `vault/decisions/`.

## Flow

1. Human talks by voice through `voice/bot.py` or by text through `scripts/cto.py`.
2. Session summaries are saved to the Markdown vault via Basic Memory or `scripts/vault.py`.
3. A PRD is written under `vault/prds/`.
4. Human approves the PRD by setting `status: approved`.
5. `scripts/coder.py` parses `## Implementation Tasks`.
6. Tasks go to the decided runner through `scripts/ao_runner.py`.
7. AO runs Claude Code/Codex in isolated git worktrees.
8. Reviews and run summaries are Markdown notes under `vault/coding-runs/`.
9. Blocked tasks are raised by `scripts/escalation.py`.
10. Human decisions are saved to the blocker note, task log, project decision register, and PRD.
11. The task resumes with `ao send` or the simulated resume path.
12. Final output is a patch plus Markdown review note for manual merge.

## Voice Architecture

Per `vault/decisions/voice-stack.md`: Pipecat with SmallWebRTC transport, Silero VAD, Smart Turn v3, faster-whisper STT, Kokoro ONNX TTS, and Claude through the Claude Code subscription as the brain. No Deepgram, ElevenLabs, Cartesia, Daily, Twilio, or cloud LLM API key is used.

## Runner And AO Provenance

Per `vault/decisions/coding-runner.md`, coding agents are Claude Code and Codex CLI workers driven by AgentWrapper/agent-orchestrator.

AO was used because the decision record pins it as the runner with worktree-per-session, Claude/Codex workers, reviewer loop, human escalation, and a verified headless CLI surface. `vault/decisions/agent-orchestrator-repo.md` records the provenance check: GitHub returned a 301 redirect from `ComposioHQ/agent-orchestrator` to `AgentWrapper/agent-orchestrator`, and the canonical repo returned 200. The pinned build is `@aoagents/ao@0.10.1-nightly`, commit `249c67046d14809943d228b01eefedb10821e5dc`.

## State Boundaries

- Vault: decisions, PRDs, session summaries, blocker reports, coding run reports.
- SQLite: runtime task state and logs only.
- Git worktrees: isolated agent work.
- Patches: `~/.ai-cto/patches/`.

No custom vector DB, custom worktree engine, custom voice stack, auto-merge, or auto-deploy.

## Unresolved Future Upgrades

- Live AO run after one-time Claude Code OAuth in WSL user `aicto`.
- More complete reviewer loop once AO live runs finish end to end.
- Optional local Ollama brain if voice latency matters more than Claude Code depth.
- Graphiti remains V3-or-later only per `vault/decisions/graphiti-status.md`.

