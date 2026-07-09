# CTO ↔ Coding-Runner glue (`coder.py` + `ao_runner.py`)

Wires the AI-CTO memory vault to the decided parallel coding-agent runner so the CTO/manager can
dispatch tasks and collect results **without a human driving the runner by hand**.

## Which runner, and why

**AgentWrapper/agent-orchestrator (AO)** — this is the runner **on record**, not a choice made by
this script:

- `decisions/coding-runner.md` → coding agents are Claude Code + Codex workers driven by AO.
- `decisions/agent-orchestrator-repo.md` → independently pins `AgentWrapper/agent-orchestrator`
  (Apache-2.0; ComposioHQ URL is a redirect to the same repo).

AO was chosen because it is the only surveyed tool with worktree-per-session **and** an automated
reviewer loop **and** human escalation **and** a **headless/CLI surface** we can drive
programmatically. `johannesjo/parallel-code` was rejected earlier: no documented headless/API mode
(GUI-only), so it can't be driven by a supervisor. This glue is runner-agnostic; all AO specifics
live in **one file** (`ao_runner.py`) so a runner change touches only that file.

## Design

```
vault/prds/*.md (approved PRD)
        │  parse "## Implementation Tasks" checklist  → small tasks
        ▼
   coder.py  ──dispatch──►  ao_runner.py  ──►  AO: ao spawn (headless, 1 worktree/task)
        │                        │
        │  runtime state         │  status via `ao session get --json`
        ▼                        ▼
  ~/.ai-cto/coder.db     git diff on the session worktree  (read-only, NO merge/push)
        │                        │
        └────── collect ─────────┘
                    │
                    ├─ ~/.ai-cto/patches/<task>.patch   (the diffs)
                    └─ vault/coding-runs/<run>.md        (human review summary)
```

- **`ao_runner.py`** — the *only* file that knows AO's command surface (`AO_CMDS`, `AGENT_MAP`,
  `STATUS_MAP`). Result capture is done by us with `git diff base...HEAD` on the session's
  worktree, so it's identical whether the runner is AO or anything else.
- **`coder.py`** — runner-agnostic glue: read PRD → split tasks → dispatch → poll → collect →
  write vault summary. Runtime metadata in **SQLite outside the vault**.

## PRD contract

A PRD is a vault Markdown note with frontmatter `type: prd` and **`status: approved`**, plus an
`## Implementation Tasks` checklist. Each `- [ ]` bullet becomes one small task. Optional per-line
hints: `(agent: codex|claude)` and `(dir: <relpath>)`. See `vault/prds/sample-greet-cli.md`.

## Usage

```bash
# 1. dispatch an approved PRD against a target git repo
python coder.py dispatch --prd vault/prds/sample-greet-cli.md --repo /path/to/repo \
                         [--default-agent claude] [--project ai-cto] [--force]

# 2. refresh task statuses from the runner
python coder.py poll   [--run <run_id>]     # defaults to latest run

# 3. capture diffs + write the vault review summary
python coder.py collect [--run <run_id>]

# 4. run the final output / tests in every task worktree
python coder.py preview --run <run_id> --cmd "python -m pytest -q"

# 5. send human feedback back to one task
python coder.py feedback --task <task_id> --message "Fix the failing preview and rerun."

# optional GitHub flow (requires `gh` logged in)
python coder.py pr --task <task_id>
python coder.py merge-pr --pr <number-or-url> --repo /path/to/repo --method squash

# inspect anytime
python coder.py status [--run <run_id>]
```

**Dispatching Codex vs Claude Code:** set it per task in the PRD (`(agent: codex)` /
`(agent: claude)`), or set the run default with `--default-agent`. `coder.py` maps these to AO
**agent-plugin** names via `AGENT_MAP` in `ao_runner.py` (`claude → claude-code`, `codex → codex`);
verified against `ao spawn --help` in @aoagents/ao@0.10.1-nightly.

## Verified AO surface (@aoagents/ao@**0.10.1-nightly**, WSL2 Ubuntu, 2026-07-07)

The working build is the **headless-first nightly**, not stable 0.10.0 (which can't start headless on
Linux). `ao_runner.py` drives these **verified** commands (all isolated in `AO_CMDS`):

```
ao status [--json]                              # orchestrator health (started by `ao start`, headless)
ao project add <abs-path>                       # register repo (id is auto-assigned; no --id flag)
ao project ls                                   # read the auto id (TEXT: "<id> (<name>)")
ao project set-default <id>                     # spawn targets the default project
ao spawn --agent <plugin> --prompt "<text>"     # session in a fresh worktree (no --project/--branch)
ao session ls --json -p <id>                    # find/poll the session (NO `ao session get` exists)
ao session kill <sid>                           # stop
```
Session JSON: `id` / `workspacePath` / `branch` / `status` (notifier noise precedes the JSON). `spawn`
prints no id, so the new session is found by snapshot-diffing `session ls` ids. We then compute the
diff ourselves: `git -C <worktree> diff <base>...HEAD` (read-only, no merge).

## Running AO for real (WSL2) — current status

Environment is **provisioned and proven headless** on the nightly: Ubuntu WSL2 + tmux + Node 22 +
`@aoagents/ao@0.10.1-nightly`, running as the **non-root user `aicto`**. `ao start` brings up a
headless orchestrator + `:3000` dashboard (no GUI); `ao spawn` creates real worktree sessions purely
via CLI, and the root-permissions blocker is gone. **Exactly one interactive step remains** (see
`RUNBOOK-ao-wsl2.md`):

1. **Log the harness in — one-time, interactive.** Run `claude` once as `aicto` and complete the
   OAuth login (uses the Claude subscription, not an API key). Onboarding is pre-seeded so it goes
   straight to the login prompt. (`codex` optional, if dispatching Codex tasks.)

The orchestrator itself is headless: keep `ao start <repo>` running (the supervisor will own it — a
one-shot background launch does not survive). Session JSON keys are confirmed; on the first real run
just confirm the live **status strings** and extend `STATUS_MAP` if needed.

## Sample flow (verified in simulated mode)

```
python coder.py dispatch --prd vault/prds/sample-greet-cli.md --repo ~/.ai-cto/sample-repo
python coder.py poll
python coder.py collect
```

Produces: 3 real git worktrees (one per task, correct agent tags), 3 real `.patch` files under
`~/.ai-cto/patches/`, SQLite state in `~/.ai-cto/coder.db`, and a review note at
`vault/coding-runs/<run>.md` flagged `review-needed` with **no auto-merge**.

## Known limitations

- **No real coding run yet — one interactive gate remains.** Everything else is proven headless on
  the nightly as `aicto` (orchestrator up, session spawned into a real worktree). The remaining gate
  is the one-time **Claude Code OAuth login** for `aicto`. Until then the sample runs in **simulated
  mode** (real worktrees + diffs, **stubbed agent brain** — clearly marked, never real code).
- **`STATUS_MAP` may need one tweak.** Session JSON keys (`id`/`workspacePath`/`branch`/`status`) are
  confirmed, but the full set of `status` strings a working agent emits wasn't observed to completion;
  extend `STATUS_MAP` in `ao_runner.py` if a live value maps wrong.
- **Orchestrator must be kept alive.** `ao start <repo>` is long-running; a one-shot background
  launch does not survive. The supervisor will own it.
- **GitHub token optional here.** AO's PR/review loop needs `AO_GITHUB_TOKEN`/`gh auth login`, but
  this glue does no-merge local diff capture and doesn't require it.
- **No auto-merge by design.** The glue only reads diffs unless you explicitly run `merge-pr`.
  Merging remains a manual human step.
- **PR/merge commands are explicit.** `pr` and `merge-pr` only run when directly invoked and depend
  on the already-installed GitHub CLI (`gh`); no GitHub API client is added here.
- **Vault access from WSL** uses `/mnt/e/...` (DrvFs) — fine for small Markdown notes; target
  *repos* should live on the WSL filesystem, not `/mnt`.
