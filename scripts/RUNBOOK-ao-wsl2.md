# Runbook: bring AO live under WSL2, flip `ao_runner` to `live`

Goal: get `python coder.py dispatch ...` running real Claude Code / Codex agents in AO worktrees,
instead of simulated mode. Context: [[Decision - WSL vs Native]], [[Decision - Agent Orchestrator Repo]].

## Status (2026-07-07)

**DONE — provisioned & verified (headless, nightly):**
- ✅ Ubuntu WSL2 distro; `tmux 3.6`, `git`, Node 22, Python 3; Docker Desktop WSL2 backend.
- ✅ **`@aoagents/ao@0.10.1-nightly`** installed (headless-first; npm `latest`=0.10.0 is broken on
  Linux). Needs `build-essential` (compiles `node-pty`).
- ✅ **Non-root user `aicto`** (passwordless sudo) — root triggers Claude Code's
  `--dangerously-skip-permissions` refusal; running as `aicto` fixes it.
- ✅ Sample repo on the **WSL fs**: `/home/aicto/sample-repo` (branch `main` — AO requires a
  resolvable `main`).
- ✅ `ao start /home/aicto/sample-repo` → **headless orchestrator + dashboard `:3000`, "✓ Startup
  complete"**, config auto-written, project `sample-repo_abd90755e4`. No GUI, no AppImage.
- ✅ `ao spawn --agent claude-code --prompt …` → session `sr-1`, real worktree + tmux, **pure CLI**;
  the agent launched and began editing (root refusal gone).
- ✅ `ao_runner.py` **re-fitted to the nightly surface**; parsers verified vs real output; sim flow
  still passes.

**REMAINING — exactly ONE interactive human step: log Claude Code in for `aicto`.**

## 1. Log the harness in *(one-time, interactive — the only human gate)*
Onboarding is pre-seeded (`~/.claude.json` has `theme`+`hasCompletedOnboarding`), so this goes
straight to the OAuth prompt. Uses your Claude **subscription**, not an API key.
```bash
wsl -d Ubuntu -u aicto        # open an interactive shell as aicto
claude                        # complete the OAuth login (browser/paste), then /exit
claude --version              # VERIFY: prints a version
ls ~/.claude/.credentials.json  # VERIFY: credentials now exist
```
> Optional Codex path: install Codex CLI in WSL as `aicto` and run its login, if dispatching `codex`.

## 2. Ensure the orchestrator is running *(headless — no GUI)*
```bash
# as aicto, from the repo dir; keep this process ALIVE (a one-shot nohup does NOT survive):
cd /home/aicto/sample-repo && ao start /home/aicto/sample-repo   # run under a persistent shell / the supervisor
ao status --json               # VERIFY from the repo dir: dashboard/sessions report
```

## 3. Run the real flow *(inside WSL, as `aicto`)*
```bash
cd "/mnt/e/Projects/AI CTO/scripts"
export AI_CTO_VAULT="/mnt/e/Projects/AI CTO/vault"
python3 ao_runner.py                 # VERIFY: "AO adapter mode: live"
python3 coder.py dispatch --prd "$AI_CTO_VAULT/prds/<approved-prd>.md" --repo /home/aicto/sample-repo
python3 coder.py poll                 # repeat until tasks leave "running"; "blocked" => needs input
python3 coder.py collect              # writes real patches + vault/coding-runs/<run>.md (no auto-merge)
```

## 4. First-live-session verification (one-time)
Session JSON keys are already confirmed (`id`, `workspacePath`, `branch`, `status`). On the first
real run just confirm the **status strings** the agent actually emits (`ao session ls --json -p <id>`)
and extend `STATUS_MAP` in `ao_runner.py` if any value maps to `running` that shouldn't.

## 6. Human review (unchanged)
Open `vault/coding-runs/<run>.md`, inspect each `~/.ai-cto/patches/*.patch`, merge approved work
manually. The glue never merges or pushes.

## After a successful live run — update the record
- `ao_runner.py`: commit any `STATUS_MAP` corrections from step 4.
- `README-coder.md` + [[Decision - Coding Runner]]: move resolved items out of "known limitations".

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| `ao_runner` says `simulated` in WSL | `ao` not on PATH in that shell — `which ao`, or set `AO_BIN` |
| "AO orchestrator is not running" | `ao start <repo>` not running / was killed — restart it and keep it alive (step 2) |
| `ao start` fails: "Unable to resolve base ref for default branch main" | repo's default branch isn't `main` — `git branch -m main` |
| agent exits: `--dangerously-skip-permissions cannot be used with root` | running AO as **root** — run as the non-root `aicto` user |
| spawn ok but session stuck at a TUI / "blocked" | harness not signed in — do step 1 (`claude` login as aicto) |
| session id/worktree not found | JSON keys differ — fix `_find_new_session`/`_session_worktree` in `ao_runner.py` |
| one-shot `ao start &` dies immediately | a background launch needs the host `wsl.exe` kept alive — run under a persistent shell / supervisor |
| slow / missed file changes | repo under `/mnt/...` — keep repos on the WSL fs (`/home/aicto/...`) |
