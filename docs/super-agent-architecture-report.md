# Jarvis Super Agent Architecture Report

Date: 2026-07-09

## Goal

Turn the current Jarvis voice companion into a practical local super agent that can:

- manage desktop files and project files;
- open files, folders, URLs, browser tabs, and IDE workspaces;
- run commands, tests, apps, coding agents, and previews;
- create PRDs, dispatch coding work, inspect results, and create GitHub pull requests;
- read, summarize, draft, and reply to email with approval gates;
- research on the web and write source-backed reports;
- coordinate sub-agents and coding agents without pretending work happened;
- operate semi-autonomously, with explicit human approval for risky actions.

## Current Architecture Summary

The current system already has useful foundations:

- Voice runtime: `voice/desktop_voice.py` with Pipecat local audio, faster-whisper STT, Kokoro TTS, wake gate, and dashboard event streaming.
- Conversation brain: `voice/voice_brain.py` using Groq primary and OpenRouter fallback.
- Tools: memory, web search/fetch, blockers, coding dispatch, quick file edits, open project, Codex task, and open file.
- Memory: Markdown vault plus Basic Memory.
- Coding: `scripts/coder.py` and AO adapter around Claude Code / Codex workers.
- Status: SQLite task state and logs under `~/.ai-cto`.
- UI: browser dashboard at `http://localhost:7860`.

This is enough for a local voice CTO MVP. It is not yet enough for a dependable super agent.

## Key Finding

The existing architecture should not be thrown away. It should become the voice and tool adapter layer under a new supervisor layer.

The missing piece is a durable orchestrator that owns:

- task planning;
- tool selection;
- sub-agent delegation;
- progress tracking;
- approval gates;
- retries;
- verification;
- final reporting.

Right now the LLM can call tools directly, but there is no strong run ledger, no typed tool registry, no deterministic workflow graph, and no universal proof step before Jarvis claims success. That is why earlier it said an HTML file was created even when no tool had created it.

## Recommended Architecture

Use a layered architecture:

```text
Voice / Text UI
    |
    v
Jarvis Supervisor
    |
    +-- Tool Registry
    +-- Run Ledger
    +-- Approval Gate
    +-- Memory Context
    +-- Sub-Agent Router
    |
    +-- File Tools
    +-- Desktop Tools
    +-- Browser Tools
    +-- Code/Test Tools
    +-- Git/GitHub Tools
    +-- Email/Calendar Tools
    +-- Research/PRD Tools
    +-- Coding Agents: Codex, Claude Code, AO
```

## Framework Recommendation

Recommended default: keep custom thin Python tools, add LangGraph only for durable multi-step orchestration once the tool layer is stable.

Why:

- The current system is mostly local Windows automation and CLI glue. A large framework too early will add churn.
- LangGraph is a good fit for durable execution, streaming, human-in-the-loop flows, and explicit graph/state transitions.
- OpenAI Agents SDK is a good fit for tool loops, handoffs, sessions, approvals, and tracing, but it may tie the central runtime more tightly to OpenAI APIs than desired.
- Microsoft Agent Framework is now the supported successor to AutoGen/Semantic Kernel and is attractive for enterprise-grade type safety, telemetry, and state, but adopting it would be a bigger architectural shift.
- CrewAI is useful for role-based teams and quick automation flows, but it is less obviously necessary for a local desktop super-agent core.
- AutoGen should not be the new foundation; Microsoft’s own GitHub page says AutoGen is in maintenance mode.

Decision:

- V1.1: no new orchestration framework. Build the tool registry, run ledger, approval gates, and verification contracts in plain Python.
- V2: add LangGraph if workflows become multi-step and stateful enough to justify it.
- Revisit OpenAI Agents SDK if we later accept OpenAI-hosted tracing/sessions as the primary control plane.

## Required Super-Agent Tools

### File And Desktop Tools

| Tool | Purpose | Notes |
|---|---|---|
| `find_file` | Fuzzy search files/folders by name, type, recency | Use Everything CLI first, bounded walk fallback. |
| `open_file` | Open file with OS default app | Already started. Expand app targeting. |
| `open_folder` | Open folder in Explorer | Needed for “show me the folder”. |
| `open_in_ide` | Open file/folder in VS Code, Cursor, PyCharm, etc. | Use `code`, `cursor`, JetBrains launchers. |
| `read_file` | Read/summarize files | Size limits, binary detection. |
| `write_file` | Create/edit files | Approval for broad writes; verify path exists after. |
| `move_file` | Move/rename files | Confirm if overwrite/delete risk. |
| `copy_file` | Copy files/folders | Confirm large/bulk ops. |
| `delete_file` | Delete files/folders | Always require explicit confirmation. |
| `desktop_search` | Search Desktop/Downloads/Documents | Include user home roots, guarded. |
| `open_app` | Launch apps | VS Code, Cursor, Chrome, Terminal, Obsidian, Excel, Word. |

### Browser Tools

| Tool | Purpose | Notes |
|---|---|---|
| `open_url` | Open URL in browser | Simple first. |
| `web_search` | Search current web | Existing tool, improve source quality. |
| `web_fetch` | Read URL content | Existing tool. |
| `browser_open_tab` | Open/search in a browser tab | Playwright or Chrome DevTools. |
| `browser_read_page` | Read current page | Needs controlled browser. |
| `browser_click` | Click/fill/navigate | Approval for forms/submits. |
| `browser_download` | Download files | Save path tracked. |

### Code And Test Tools

| Tool | Purpose | Notes |
|---|---|---|
| `run_command` | Run command in allowed folder | Backbone tool. Guard cwd and timeout. |
| `run_tests` | Detect and run tests | pytest, unittest, npm, pnpm, vitest, playwright, cargo. |
| `run_app` | Start dev server | Return URL and PID. |
| `stop_process` | Stop started dev server | Only PIDs owned by Jarvis. |
| `inspect_logs` | Read recent logs/errors | Useful for “why stuck?”. |
| `codex_task` | Non-interactive Codex edits | Already started. Must verify outputs. |
| `claude_task` | Non-interactive Claude edits | Existing quick task path. |
| `coding_dispatch` | PRD/task to AO/Codex/Claude | Existing `scripts/coder.py`. |
| `preview_task` | Run preview/tests in worktree | Existing flow. |

### Git And GitHub Tools

| Tool | Purpose | Notes |
|---|---|---|
| `git_status` | Show changed files | Safe. |
| `git_diff` | Summarize changes | Safe. |
| `git_branch` | Create/switch branches | Confirm if dirty. |
| `git_commit` | Commit approved changes | Explicit approval. |
| `github_pr_create` | Push branch and create PR | Use `gh`; explicit approval. |
| `github_pr_status` | Read PR status/checks | Safe. |
| `github_pr_comments` | Read review comments | Safe. |
| `github_fix_ci` | Inspect CI logs and dispatch fix | Needs tool chain. |
| `github_issue_create` | Create issue | Confirm before submit. |

### Email And Calendar Tools

| Tool | Purpose | Notes |
|---|---|---|
| `email_search` | Search mailbox | Gmail API. |
| `email_read_thread` | Read/summarize thread | Safe-ish, private data handling. |
| `email_draft_reply` | Draft reply | Do not send automatically. |
| `email_send_reply` | Send email | Always explicit confirmation. |
| `email_label_archive` | Triage inbox | Confirmation for bulk ops. |
| `calendar_read` | Read schedule | Google Calendar API. |
| `calendar_create_event` | Create event | Confirm before create. |

### Research And Planning Tools

| Tool | Purpose | Notes |
|---|---|---|
| `research_brief` | Multi-source report | Must cite links. |
| `compare_options` | Evaluate products/frameworks/tools | Use current web. |
| `create_prd` | Turn discussion into PRD | Existing `scripts/cto.py` can be expanded. |
| `update_prd` | Revise PRD | Must preserve decision history. |
| `task_breakdown` | Convert PRD to tasks | Existing coding runner contract. |
| `decision_note` | Save architecture decision | Vault decision template. |

### Memory And Status Tools

| Tool | Purpose | Notes |
|---|---|---|
| `search_memory` | Search project memory | Existing. |
| `write_memory` | Save durable fact | Existing. |
| `current_status` | Show active runs/tasks | Existing. |
| `run_ledger_get` | Show exact current operation | New, essential. |
| `run_ledger_update` | Record tool call/result/proof | New, essential. |
| `cancel_current_task` | Stop active agent/tool run | Needed for “leave whatever you were doing”. |

## Non-Negotiable Safety Rules

Jarvis must ask for explicit confirmation before:

- deleting files;
- moving large folders;
- overwriting existing important files;
- sending emails;
- pushing commits;
- creating or merging PRs;
- installing packages globally;
- running commands outside allowed roots;
- using browser automation to submit forms;
- spending money or using paid APIs.

Jarvis may do without confirmation:

- read files in allowed roots;
- open files/folders/URLs;
- run tests in the current project;
- create small files in a clearly requested target folder;
- draft emails without sending;
- create PRDs/notes;
- search the web;
- summarize data.

## Verification Contract

Every action tool should return:

```json
{
  "ok": true,
  "action": "created_file",
  "target": "E:\\Projects\\test_folder\\index.html",
  "proof": "file_exists",
  "summary": "Created index.html"
}
```

Jarvis must not say “done” unless:

- the tool returned `ok: true`;
- the result includes proof;
- the proof was written to the run ledger.

For file creation, proof is file exists.
For tests, proof is command exit code and output.
For PRs, proof is PR URL.
For email send, proof is Gmail API message id.
For browser actions, proof is current URL/page state.

## Run Ledger

Add a small SQLite-backed run ledger:

Tables:

- `runs`: id, user_request, status, started_at, completed_at, summary.
- `steps`: id, run_id, tool, args_redacted, status, output_summary, proof, started_at, completed_at.
- `artifacts`: id, run_id, kind, path_or_url, description.
- `approvals`: id, run_id, action, risk, approved_by_user, timestamp.

This becomes the source for:

- “What are you doing?”
- “Did you finish?”
- “What file did you create?”
- “Stop current task.”
- “Undo that if possible.”

## Architecture Decision

The current architecture is not enough by itself for the requested super-agent.

Keep:

- Pipecat voice stack;
- Basic Memory + vault;
- Groq/OpenRouter voice brain;
- Claude Code and Codex as coding workers;
- `scripts/coder.py` and AO adapter for PRD-based coding runs;
- dashboard and blocker watcher.

Improve:

- Add a supervisor layer above `voice_brain.py`.
- Move direct tools into a typed tool registry.
- Add run ledger and verification contract.
- Add approval gate.
- Add browser, GitHub, Gmail, and test-runner tools.
- Add sub-agent routing only after the tool layer is stable.

Defer:

- Full LangGraph migration until workflows need durable branching/retry/human approval state beyond simple tool calls.
- Replacing AO until it blocks real coding runs after OAuth setup.
- Custom vector DB.
- Auto-merge and auto-deploy.

## Suggested Implementation Order

### Phase 1: Reliability Foundation

1. Add run ledger.
2. Add tool result schema.
3. Make every tool write proof.
4. Add `cancel_current_task`.
5. Update voice prompt: never claim success without proof.

### Phase 2: Local Desktop/File Superpowers

1. Expand `find_file`, `open_file`, `open_folder`.
2. Add `open_in_ide`.
3. Add guarded `read_file`, `write_file`, `move_file`, `delete_file`.
4. Add Desktop/Downloads/Documents roots.

### Phase 3: Code Execution

1. Add `run_command`.
2. Add `run_tests`.
3. Add `run_app` and `stop_process`.
4. Add `inspect_logs`.
5. Wire all outputs into run ledger.

### Phase 4: Browser And Research

1. Add `open_url`.
2. Add Playwright browser control.
3. Add research report tool.
4. Add PRD generation from research.

### Phase 5: GitHub

1. Add `git_status`, `git_diff`.
2. Add `github_pr_create`.
3. Add `github_pr_status`.
4. Add CI log inspection.

### Phase 6: Email And Calendar

1. Gmail OAuth setup.
2. Search/read/summarize.
3. Draft replies.
4. Confirm-send.
5. Calendar read/create.

### Phase 7: Orchestration Upgrade

Only after the above tools work:

1. Add supervisor state machine.
2. Add LangGraph for durable multi-step workflows if needed.
3. Add specialist sub-agents:
   - File Agent
   - Browser Agent
   - Coding Agent
   - Research Agent
   - Email Agent
   - GitHub Agent
4. Keep final authority in Jarvis Supervisor.

## Framework Notes From Current Research

- LangGraph positions itself around durable execution, streaming, human-in-the-loop, and agent orchestration. That matches the future supervisor need.
- OpenAI Agents SDK is useful when the SDK should manage the tool loop, handoffs, sessions, tracing, guardrails, and resumable approvals.
- Microsoft Agent Framework is the current successor to AutoGen and Semantic Kernel, with state management, type safety, filters, telemetry, and model support.
- AutoGen is now marked maintenance mode on Microsoft's GitHub, so it should not be the foundation for new work.
- CrewAI is suitable for role-based multi-agent workflows and has guardrails, memory, knowledge, and observability, but it is not necessary before the local tool layer is reliable.

Sources:

- LangGraph overview: https://docs.langchain.com/oss/python/langgraph/overview
- OpenAI Agents SDK guide: https://developers.openai.com/api/docs/guides/agents
- OpenAI Agents Python docs: https://openai.github.io/openai-agents-python/
- Microsoft Agent Framework overview: https://learn.microsoft.com/en-us/agent-framework/overview/
- Microsoft AutoGen GitHub maintenance note: https://github.com/microsoft/autogen
- CrewAI docs: https://docs.crewai.com/

## Final Recommendation

Do not start by replacing the whole system with LangGraph or another framework.

Start by making Jarvis honest and capable:

1. typed tools;
2. proof-returning tool results;
3. run ledger;
4. approval gates;
5. local desktop/file/browser/code/GitHub/email tools.

Then add LangGraph if the supervisor logic becomes too complex for plain Python.

This path keeps the working voice and coding system while moving toward the super-agent architecture safely.
