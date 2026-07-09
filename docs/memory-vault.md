# Memory Vault — Setup, Commands & Architecture

The AI-CTO companion's long-term memory. Chosen engine: **Basic Memory**
(`basicmachines-co/basic-memory`) over a local, Obsidian-compatible Markdown vault.
No custom storage layer — see [[Decision - Memory Engine]] (`vault/decisions/memory-engine.md`).

## 1. What was set up

| Thing | Value |
|---|---|
| Engine | Basic Memory 0.22.1 (`uv tool install basic-memory`) |
| Vault (Obsidian-compatible) | `E:\Projects\AI CTO\vault` (relocated to C: on 2026-07-07 during the E: drive failure, moved back 2026-07-08; E: disk has intermittent errors — keep backups) |
| BM project name | `ai-cto` (default project) |
| Index / DB | `~/.basic-memory/` (SQLite + full-text + local vector embeddings) |
| Wrapper | `scripts/vault.py` (thin conventions layer, optional) |
| CLI shims | `~/.local/bin/basic-memory.exe`, `bm.exe` (on PATH after a shell restart) |

### Vault layout
```
vault/
  index.md              # map-of-content / start here
  profile.md            # who the human is
  active-projects.md    # portfolio index
  open-loops.md         # unresolved threads (checklist)
  decisions.md          # human-readable decision register (index)
  preferences.md        # durable working preferences
  workflows/agentic-coding.md
  projects/cto-agent.md
  decisions/            # machine-checkable, ONE decision per file
    memory-engine.md          (decided)
    agent-orchestrator-repo.md(decided)
    coding-runner.md          (decided)
    voice-stack.md            (decided)
    graphiti-status.md        (deferred)
  sessions/             # session summaries (created on first `vault.py summary`)
```

## 2. Decision-tracking convention (relied on by future sessions)

- Source of truth is the **`vault/decisions/` folder**, one file per decision key.
- **File `decisions/<key>.md` exists ⇒ decision made. Absent ⇒ not yet decided.**
- Status inside is `Decided` or `Deferred` (deferred still = a made decision, "not now").
- Each file records: the decision, date, status, rationale, and pinned repo URL/version.
- `decisions.md` is the human-readable register that mirrors the folder — update its table
  when you add a file.
- Programmatic check: the existence of `vault/decisions/<key>.md`
  (or `python scripts/vault.py decisions`).

## 3. Reproduce the setup (fresh machine)

```powershell
uv tool install basic-memory                 # installs `basic-memory` + `bm`
uv tool update-shell                          # add ~/.local/bin to PATH, then restart shell
basic-memory project add ai-cto "E:\Projects\AI CTO\vault"
basic-memory reindex --project ai-cto         # first index; downloads a local embedding model once
```
> There is **no** `basic-memory sync` command in 0.22.x. While the MCP server runs, changes
> index automatically (file watcher). For a one-shot CLI index with no server running, use
> `basic-memory reindex --project ai-cto`. `basic-memory status --wait` blocks until indexed.

## 4. Commands exposed to the companion agent

### A. Native Basic Memory MCP (preferred — for Claude Code / the voice brain)
Register the server once in Claude Code:
```powershell
claude mcp add basic-memory -- basic-memory mcp --project ai-cto
# (or, without PATH shims:)  claude mcp add basic-memory -- uvx basic-memory mcp --project ai-cto
```
Key MCP tools the agent gets: `write_note`, `read_note`, `edit_note`, `search_notes`,
`build_context`, `recent_activity`, `list_directory`, `delete_note`. These are the
primitives for read/write/search over memory — no custom code needed.

### B. Native Basic Memory CLI (same tools, for scripts/supervisor)
```powershell
bm tool write-note --title "T" --folder notes --content "..."   # or pipe via stdin
bm tool search-notes "zero paid APIs"
bm tool build-context "memory://decisions/memory-engine"        # note + its relations/backlinks
bm tool recent-activity
bm status                                                        # sync state
```

### C. Convention wrapper (`scripts/vault.py`) — thin, optional
Encodes folders/templates so every session writes memory consistently:
```powershell
python scripts/vault.py summary "kicked off memory setup" "Body markdown..."   # -> sessions/
python scripts/vault.py decide escalation-transport "Use win11toast + local pipecat session"
python scripts/vault.py context "voice stack"     # search; no arg -> recent activity
python scripts/vault.py loops                      # list open `- [ ]` items
python scripts/vault.py decisions                  # list decided keys (the register)
```

## 5. Architecture note

- **Storage = plain Markdown + `[[wikilinks]]`.** The vault is a normal Obsidian vault; a
  human can open and edit it. Nothing is locked in a proprietary store.
- **Index = Basic Memory's SQLite DB** (`~/.basic-memory/`), giving full-text search,
  relations, and backlinks derived from the wikilinks. Semantic search uses a **local**
  embedding model (downloaded once from HuggingFace, then fully offline) — no paid API,
  satisfying the project's zero-paid-API hard constraint.
- **Access = MCP first, CLI/wrapper second.** The companion's "brain" (Claude via the
  Claude Code subscription) talks to memory through the Basic Memory MCP server. Scripts and
  the future supervisor use the CLI. `scripts/vault.py` is only a conventions veneer over the
  CLI; delete it and nothing breaks.
- **Decisions are files, not prose.** Machine-checkable one-file-per-decision under
  `decisions/` lets a later session in a fresh context answer "is X decided?" by a file
  existence check, without re-reading narrative. `decisions.md` keeps it human-readable.
- **Sync model:** no daemon required for editing. Run `bm reindex` after bulk file edits made
  outside the server, or keep the MCP server running for live indexing.
