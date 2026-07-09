"""brain.py - the voice companion's CTO brain.

Claude via the **Claude Code subscription** (Claude Agent SDK -> local `claude` CLI),
NOT a metered API key. Memory access is the **basic-memory MCP server** pointed at the
vault, so the brain reads/writes long-term memory with the same tools as a normal
Claude Code session.

Pipecat integration: `ClaudeCTOBrain` is a FrameProcessor that fills the LLM slot.
It consumes `LLMContextFrame` from the user context aggregator (which handles VAD /
Smart Turn / transcription aggregation) and emits the standard
LLMFullResponseStart / LLMTextFrame / LLMFullResponseEnd sequence that the TTS service
and assistant aggregator expect.

UI side channel: every user transcript, streamed answer chunk, and memory (MCP) tool
call is reported through an async `events` callback so the browser page can render the
transcript and the "memory used" panel.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import escalation  # blocker lifecycle (scripts/escalation.py)
import coder  # coding run feedback / preview glue
import jarvis

import actions  # voice-driven file edits + project launching (Phase 3)
from memory_observer import AutoMemory

from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

PROJECT_ROOT = Path(__file__).resolve().parents[1]  # the AI CTO project root
BM_PROJECT = "ai-cto"

SYSTEM_PROMPT = f"""You are the user's CTO companion, speaking over voice.

You have the project's long-term memory (an Obsidian vault) available through the
basic-memory MCP tools (search_notes, read_note, build_context, recent_activity,
write_note). Before answering questions about the project, its decisions, or its
history, SEARCH MEMORY FIRST. Cite which note you used in passing ("per the voice-stack
decision...") so the human knows the answer is grounded.

Voice rules — your words are synthesized aloud:
- Answer in 1-4 short sentences unless explicitly asked to go deep.
- Plain spoken prose only: no markdown, no bullet lists, no headings, no code blocks,
  no emoji. Spell out abbreviations the first time.
- If asked to remember something, save it with write_note (folder `voice-notes`)
  or remember_note and confirm aloud.
- If asked to start coding work, first say you understood, then call start_coding_task.
  The tool records progress so future "what's going on" questions can be answered.
- If asked what's going on, call current_status and answer from that saved state.
- If asked for current/latest/internet information or given a URL, use web_search or web_fetch.
- If asked to make a quick change to a file or folder ("edit X in the Y folder",
  "fix the typo in that file"), first say what you're doing, then call quick_task with
  the folder/file name as target and the change as instruction. Report the worker's
  result briefly. Use start_coding_task instead for larger multi-file work.
- If asked to open or work on a project in Claude or Codex ("open the summarizer
  project in codex"), call open_project with the project name and the agent, then
  confirm the session opened.

Blocker escalation: coding agents sometimes get BLOCKED and need a human decision.
- If the human asks about blockers (or you are told one is pending), call
  list_blockers and explain using the report's "Voice summary", then the options and
  your recommendation. Ask which option they choose.
- When the human states a decision (including "go with your recommendation"), call
  resolve_blocker with the task_id and the decision spelled out in full (resolve the
  words "your recommendation" into the actual recommended option text). Then confirm
  aloud that the decision is saved and the agent is resuming.
- Never merge, push, or deploy on their behalf; resolving a blocker only lets the
  agent continue working in its worktree.
- If the human asks to see final output, call preview_coding_run with the run id and
  the exact local command to run. Summarize the preview note path and the result.
- If the human gives feedback on a coding task, call submit_coding_feedback with the
  task id and the feedback. This resumes the worker path without merging or pushing.

Project context: the AI-CTO system lives at {PROJECT_ROOT}. Its hard constraint is
zero paid APIs (only the Claude and Codex subscriptions are allowed).
"""


# --- escalation tools exposed to the brain (in-process MCP server) ----------------------


@tool("list_blockers", "List pending coding-agent blockers with their full reports "
      "(task, worktree, what failed, options, recommendation).", {})
async def _list_blockers_tool(args: dict) -> dict:
    data = await asyncio.to_thread(escalation.pending_blockers_full)
    return {"content": [{"type": "text", "text": json.dumps(data, default=str)}]}


@tool("resolve_blocker", "Save the human's decision for a blocked task and resume the "
      "coding agent. Decision must be the full option text, not 'option 2'.",
      {"task_id": str, "decision": str, "rationale": str})
async def _resolve_blocker_tool(args: dict) -> dict:
    result = await asyncio.to_thread(
        escalation.resolve_and_resume,
        args["task_id"], args["decision"], args.get("rationale", ""))
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


@tool("preview_coding_run", "Run a local preview/test command inside every worktree for a coding run "
      "and write the outputs to a Markdown report.", {"run_id": str, "command": str})
async def _preview_coding_run_tool(args: dict) -> dict:
    class A:
        run = args["run_id"]
        cmd = args["command"]
        timeout = 60
    await asyncio.to_thread(coder.cmd_preview, A)
    return {"content": [{"type": "text", "text": f"Preview written for {A.run}"}]}


@tool("submit_coding_feedback", "Send human feedback to a coding task so the worker can revise. "
      "Does not merge, push, or deploy.", {"task_id": str, "message": str})
async def _submit_coding_feedback_tool(args: dict) -> dict:
    class A:
        task = args["task_id"]
        message = args["message"]
    await asyncio.to_thread(coder.cmd_feedback, A)
    return {"content": [{"type": "text", "text": f"Feedback sent to {A.task}"}]}


@tool("start_coding_task", "Create a one-task PRD and dispatch it to the coding runner. "
      "Use an empty repo string to use AI_CTO_DEFAULT_REPO.", {"title": str, "task": str, "repo": str, "agent": str})
async def _start_coding_task_tool(args: dict) -> dict:
    result = await asyncio.to_thread(
        coder.start_task,
        args.get("repo") or os.environ.get("AI_CTO_DEFAULT_REPO"),
        args["title"], args["task"], args.get("agent") or "claude")
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


@tool("current_status", "Read saved Jarvis task/activity status so the user can ask mid-task "
      "what is going on.", {})
async def _current_status_tool(args: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(coder.status_snapshot(), default=str)}]}


@tool("quick_task",
      "Make a quick, scoped change to a file or folder anywhere in the allowed roots "
      "(the E: drive by default). 'target' is a folder name, a file name, or a path; "
      "'instruction' is the change to make. A headless Claude worker edits in place. "
      "Use this for small edits, NOT for full multi-file features (use start_coding_task "
      "for those).",
      {"target": str, "instruction": str})
async def _quick_task_tool(args: dict) -> dict:
    result = await asyncio.to_thread(actions.quick_task, args.get("target", ""), args["instruction"])
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


@tool("open_project",
      "Open a project in an interactive Claude or Codex session in a new terminal tab. "
      "'name' is the project/folder name to resolve; 'agent' is 'claude' or 'codex'.",
      {"name": str, "agent": str})
async def _open_project_tool(args: dict) -> dict:
    result = await asyncio.to_thread(actions.open_project, args["name"], args.get("agent") or "claude")
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


@tool("remember_note", "Write a durable memory note to Basic Memory voice-notes.", {"text": str})
async def _remember_note_tool(args: dict) -> dict:
    result = await asyncio.to_thread(jarvis.remember_note, args["text"], "voice companion")
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


@tool("web_search", "Search the public web with a simple free HTML search and return source links.",
      {"query": str})
async def _web_search_tool(args: dict) -> dict:
    result = await asyncio.to_thread(jarvis.web_search, args["query"])
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


@tool("web_fetch", "Fetch a URL and return readable page text plus the source URL.", {"url": str})
async def _web_fetch_tool(args: dict) -> dict:
    result = await asyncio.to_thread(jarvis.web_fetch, args["url"])
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


ESCALATION_SERVER = create_sdk_mcp_server(
    name="escalation", version="1.0.0",
    tools=[_list_blockers_tool, _resolve_blocker_tool,
           _preview_coding_run_tool, _submit_coding_feedback_tool,
           _start_coding_task_tool, _current_status_tool, _remember_note_tool,
           _web_search_tool, _web_fetch_tool,
           _quick_task_tool, _open_project_tool])

# Only memory (MCP) tools + read-only project inspection. No shell, no file edits.
ALLOWED_TOOLS = [
    "mcp__memory__search_notes",
    "mcp__memory__read_note",
    "mcp__memory__build_context",
    "mcp__memory__recent_activity",
    "mcp__memory__write_note",
    "mcp__escalation__list_blockers",
    "mcp__escalation__resolve_blocker",
    "mcp__escalation__preview_coding_run",
    "mcp__escalation__submit_coding_feedback",
    "mcp__escalation__start_coding_task",
    "mcp__escalation__current_status",
    "mcp__escalation__remember_note",
    "mcp__escalation__web_search",
    "mcp__escalation__web_fetch",
    "mcp__escalation__quick_task",
    "mcp__escalation__open_project",
    "Read",
    "Grep",
    "Glob",
]
DISALLOWED_TOOLS = ["Bash", "Write", "Edit", "WebSearch", "WebFetch", "Task"]

EventCallback = Callable[[dict], Awaitable[None]]


def _memory_event(block: ToolUseBlock) -> dict | None:
    """Turn an MCP tool call into a 'memory used' UI event (None for non-memory tools)."""
    if not block.name.startswith(("mcp__memory__", "mcp__escalation__")):
        return None
    tool = block.name.removeprefix("mcp__memory__").removeprefix("mcp__escalation__")
    inp = block.input or {}
    detail = (
        inp.get("query")
        or inp.get("identifier")
        or inp.get("url")
        or inp.get("title")
        or inp.get("decision")
        or inp.get("task_id")
        or ", ".join(f"{k}={v}" for k, v in list(inp.items())[:3])
    )
    return {"type": "memory", "tool": tool, "detail": str(detail)[:200]}


class ClaudeCTOBrain(FrameProcessor):
    """LLM slot processor backed by a persistent Claude Code (subscription) session."""

    def __init__(self, *, events: EventCallback | None = None,
                 auto_memory: bool = True, **kwargs):
        super().__init__(**kwargs)
        self._events = events
        self._client: ClaudeSDKClient | None = None
        self._connect_lock = asyncio.Lock()
        self._response_task: asyncio.Task | None = None
        self._memory = AutoMemory(enabled=auto_memory)
        self._current_user_text = ""

    async def _emit(self, event: dict) -> None:
        if self._events:
            try:
                await self._events(event)
            except Exception as e:  # UI must never break the voice loop
                logger.warning(f"UI event callback failed: {e}")

    async def _ensure_client(self) -> ClaudeSDKClient:
        async with self._connect_lock:
            if self._client is None:
                options = ClaudeAgentOptions(
                    system_prompt=SYSTEM_PROMPT,
                    cwd=str(PROJECT_ROOT),
                    mcp_servers={
                        "memory": {
                            "type": "stdio",
                            "command": "basic-memory",
                            "args": ["mcp", "--project", BM_PROJECT],
                        },
                        "escalation": ESCALATION_SERVER,
                    },
                    allowed_tools=ALLOWED_TOOLS,
                    disallowed_tools=DISALLOWED_TOOLS,
                    max_turns=8,
                )
                client = ClaudeSDKClient(options=options)
                logger.info("Connecting Claude Code session (subscription brain)...")
                await client.connect()
                self._client = client
                logger.info("Claude Code session connected.")
        return self._client

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, InterruptionFrame):
            await self._cancel_response()
            await self.push_frame(frame, direction)
        elif isinstance(frame, LLMContextFrame):
            await self._start_response(frame)
        else:
            await self.push_frame(frame, direction)

    async def _cancel_response(self):
        if self._response_task and not self._response_task.done():
            self._response_task.cancel()
            try:
                await self._response_task
            except asyncio.CancelledError:
                pass
            if self._client:
                try:
                    await self._client.interrupt()
                except Exception as e:
                    logger.warning(f"claude interrupt failed: {e}")
        self._response_task = None

    async def _start_response(self, frame: LLMContextFrame):
        # One in-flight response at a time; a new user turn supersedes the old one.
        await self._cancel_response()

        messages = frame.context.get_messages()
        user_text = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    user_text = content
                elif isinstance(content, list):
                    user_text = " ".join(
                        p.get("text", "") for p in content if isinstance(p, dict)
                    ).strip()
                break
        if not user_text.strip():
            return

        await self._emit({"type": "user_transcript", "text": user_text})
        self._response_task = self.create_task(self._respond(user_text))

    async def _respond(self, user_text: str):
        try:
            client = await self._ensure_client()
        except Exception as e:
            logger.error(f"Claude Code session failed to start: {e}")
            await self._emit({"type": "error", "text": f"brain unavailable: {e}"})
            return

        await self.push_frame(LLMFullResponseStartFrame())
        await self._emit({"type": "bot_text_start"})
        answer_parts: list[str] = []
        try:
            await client.query(user_text)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text:
                            answer_parts.append(block.text)
                            await self.push_frame(LLMTextFrame(block.text))
                            await self._emit({"type": "bot_text", "text": block.text})
                        elif isinstance(block, ToolUseBlock):
                            event = _memory_event(block)
                            if event:
                                await self._emit(event)
        except asyncio.CancelledError:
            logger.debug("brain response cancelled (interruption)")
            raise
        except Exception as e:
            logger.error(f"brain query failed: {e}")
            await self._emit({"type": "error", "text": str(e)})
            await self.push_frame(LLMTextFrame("Sorry, my brain hit an error."))
        else:
            # Only feed complete (non-interrupted) exchanges to the memory observer.
            self._memory.record(user_text, "".join(answer_parts))
        finally:
            await self.push_frame(LLMFullResponseEndFrame())
            await self._emit({"type": "bot_text_end"})

    async def cleanup(self):
        await super().cleanup()
        await self._cancel_response()
        await self._memory.aclose()
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
