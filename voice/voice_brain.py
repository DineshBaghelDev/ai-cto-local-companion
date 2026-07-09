"""voice_brain.py - Groq-primary / OpenRouter-fallback conversational brain.

Replaces the Claude-Code-subscription brain (brain.ClaudeCTOBrain) in the voice
path. Claude Code / Codex stay wired in for actual coding work (start_coding_task,
quick_task, open_project all still subprocess those CLIs) — only the *conversation*
model moves to a cloud LLM API, per the user-authorized exception to the
zero-paid-API constraint (see vault/decisions/voice-brain.md).

Groq is primary (OpenAI-compatible, native streaming + native tool calling, fast
LPU inference, generous free tier). OpenRouter is a same-session failover: if a
Groq call errors (quota, rate limit, outage), FailoverLLMService switches to
OpenRouter for the rest of the session and logs once, so voice never goes fully
silent the way it did when Claude quota hit zero.

Tools are the same actions the old MCP tool set exposed (memory, escalation,
coding dispatch, web), reimplemented as plain OpenAI-style FunctionSchema tools
that call the same underlying plain Python functions in scripts/{escalation,
coder,jarvis}.py and voice/actions.py — see the mapping table in the plan.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from collections.abc import Awaitable, Callable
from pathlib import Path

from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.llm import OpenAILLMService

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import escalation  # noqa: E402  (blocker lifecycle, scripts/escalation.py)
import coder  # noqa: E402  (coding run glue)
import jarvis  # noqa: E402  (memory write + web helpers)
import supervisor  # noqa: E402  (run ledger + proof contract)

import actions  # noqa: E402  (voice-driven file edits + project launching)
from memory_observer import AutoMemory  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BM_PROJECT = "ai-cto"

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"
DEFAULT_OPENROUTER_MODEL = "meta-llama/llama-3.3-70b-instruct:free"

SYSTEM_PROMPT = f"""You are the user's CTO companion, speaking over voice.

You have tools to search and write the project's long-term memory (an Obsidian
vault): search_memory and remember_note. Use search_memory when the question is
about the project, its history, or a past decision — you don't need to search
memory for casual conversation.

Voice rules — your words are synthesized aloud:
- Answer in 1-4 short sentences unless explicitly asked to go deep.
- Plain spoken prose only: no markdown, no bullet lists, no headings, no code blocks,
  no emoji. Spell out abbreviations the first time.
- If asked to remember something, call remember_note and confirm aloud.
- If asked to start coding work, first say you understood, then call start_coding_task.
  The tool records progress so future "what's going on" questions can be answered.
- If asked what's going on, call current_status and answer from that saved state.
- If asked to stop or cancel current work, call cancel_current_task and be explicit
  about whether anything was actually stopped.
- Never say an action is done unless the tool result has ok true and a non-empty proof.
- If asked for current/latest/internet information or given a URL, use web_search or web_fetch.
- If asked to make a quick change to a file or folder ("edit X in the Y folder",
  "fix the typo in that file"), first say what you're doing, then call quick_task with
  the folder/file name as target and the change as instruction. Report the worker's
  result briefly. Use start_coding_task instead for larger multi-file work.
- If asked to open or work on a project in Claude or Codex ("open the summarizer
  project in codex"), call open_project with the project name and the agent, then
  confirm the session opened.
- If asked to open a file, show a file, or find the HTML file in the test folder,
  call open_file. Do not use open_project for files.
- If asked to find/read/write/open folders, URLs, run commands, or run tests, use the
  matching local tool and report the proof briefly.
- If asked to use Codex to create or change files in a folder, call codex_task.
  Never claim a file was created, opened, or changed unless quick_task or codex_task
  returned ok true with proof.

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

Project context: the AI-CTO system lives at {PROJECT_ROOT}. Actual coding work runs
through the Claude Code and Codex subscriptions in isolated worktrees; you (the voice
brain) only dispatch and report on that work, you don't write code yourself.
"""


# ---- tool handlers: thin async wrappers around existing plain functions -----------


def _memory_event(tool: str, kwargs: dict, result) -> dict:
    detail = (
        kwargs.get("query")
        or kwargs.get("target")
        or kwargs.get("url")
        or kwargs.get("name")
        or kwargs.get("decision")
        or kwargs.get("task_id")
        or ", ".join(f"{k}={v}" for k, v in list(kwargs.items())[:3])
    )
    return {"type": "memory", "tool": tool, "detail": str(detail)[:200]}


def _make_handler(tool_name: str, sync_fn, emit_event):
    """Wrap a sync function as a pipecat FunctionCallParams handler."""

    async def handler(params: FunctionCallParams) -> None:
        kwargs = dict(params.arguments or {})
        try:
            result = await asyncio.to_thread(supervisor.run_tool, tool_name, kwargs, sync_fn)
        except Exception as e:  # a bad tool call must not break the voice session
            logger.error(f"tool {tool_name} failed: {e}")
            await params.result_callback({"ok": False, "error": str(e), "proof": "exception"})
            return
        if emit_event:
            await emit_event(_memory_event(tool_name, kwargs, result))
        await params.result_callback(result if isinstance(result, dict) else {"result": result})

    return handler


def _preview_coding_run(run_id: str, command: str) -> dict:
    ns = types.SimpleNamespace(run=run_id, cmd=command, timeout=60)
    coder.cmd_preview(ns)
    return {"ok": True, "action": "preview_coding_run", "target": run_id,
            "summary": f"preview written for {run_id}", "proof": f"preview_command={command}"}


def _submit_coding_feedback(task_id: str, message: str) -> dict:
    ns = types.SimpleNamespace(task=task_id, message=message)
    coder.cmd_feedback(ns)
    return {"ok": True, "action": "submit_coding_feedback", "target": task_id,
            "summary": f"feedback sent to {task_id}", "proof": "feedback_recorded"}


def _start_coding_task(title: str, task: str, repo: str = "", agent: str = "claude") -> dict:
    result = coder.start_task(repo or os.environ.get("AI_CTO_DEFAULT_REPO"), title, task, agent or "claude")
    if result.get("ok"):
        result.update({"action": "start_coding_task", "target": result.get("run_id", ""),
                       "summary": f"Started coding run {result.get('run_id')}",
                       "proof": f"run_id={result.get('run_id')} prd={result.get('prd')}"})
    return result


def _search_memory(query: str) -> dict:
    return {"ok": True, "action": "search_memory", "target": query,
            "context": jarvis.tiny_memory_context(query), "summary": "memory context returned",
            "proof": "context_returned"}


def _current_status() -> dict:
    snap = coder.status_snapshot()
    snap["supervisor"] = supervisor.current_run_snapshot()
    snap.update({"ok": True, "action": "current_status", "summary": "status snapshot returned",
                 "proof": "snapshot_returned"})
    return snap


TOOL_SPECS: list[tuple[str, str, dict, list[str], object]] = [
    (
        "list_blockers",
        "List pending coding-agent blockers with their full reports (task, worktree, "
        "what failed, options, recommendation).",
        {},
        [],
        lambda: {"ok": True, "action": "list_blockers", "summary": "blockers returned",
                 "proof": "snapshot_returned", "blockers": escalation.pending_blockers_full()},
    ),
    (
        "resolve_blocker",
        "Save the human's decision for a blocked task and resume the coding agent. "
        "Decision must be the full option text, not 'option 2'.",
        {
            "task_id": {"type": "string"},
            "decision": {"type": "string"},
            "rationale": {"type": "string"},
        },
        ["task_id", "decision"],
        lambda task_id, decision, rationale="": escalation.resolve_and_resume(task_id, decision, rationale),
    ),
    (
        "preview_coding_run",
        "Run a local preview/test command inside every worktree for a coding run and "
        "write the outputs to a Markdown report.",
        {"run_id": {"type": "string"}, "command": {"type": "string"}},
        ["run_id", "command"],
        _preview_coding_run,
    ),
    (
        "submit_coding_feedback",
        "Send human feedback to a coding task so the worker can revise. Does not "
        "merge, push, or deploy.",
        {"task_id": {"type": "string"}, "message": {"type": "string"}},
        ["task_id", "message"],
        _submit_coding_feedback,
    ),
    (
        "start_coding_task",
        "Create a one-task PRD and dispatch it to the coding runner (Claude Code or "
        "Codex). Use an empty repo string to use the default repo.",
        {
            "title": {"type": "string"},
            "task": {"type": "string"},
            "repo": {"type": "string"},
            "agent": {"type": "string", "enum": ["claude", "codex"]},
        },
        ["title", "task"],
        _start_coding_task,
    ),
    (
        "current_status",
        "Read saved Jarvis task/activity status so the user can ask mid-task what is going on.",
        {},
        [],
        _current_status,
    ),
    (
        "cancel_current_task",
        "Cancel the current Jarvis-owned process if one is tracked. Does not stop unrelated processes.",
        {},
        [],
        lambda: supervisor.cancel_current_task(),
    ),
    (
        "find_file",
        "Find files or folders by name under allowed roots. kind can be any, file, or folder.",
        {"query": {"type": "string"}, "kind": {"type": "string", "enum": ["any", "file", "folder"]}},
        ["query"],
        lambda query, kind="any": actions.find_file(query, kind or "any"),
    ),
    (
        "quick_task",
        "Make a quick, scoped change to a file or folder anywhere in the allowed roots "
        "(the E: drive by default). 'target' is a folder name, a file name, or a path; "
        "'instruction' is the change to make. A headless Claude worker edits in place. "
        "Use this for small edits, NOT for full multi-file features (use start_coding_task "
        "for those).",
        {"target": {"type": "string"}, "instruction": {"type": "string"}},
        ["target", "instruction"],
        lambda target, instruction: actions.quick_task(target, instruction),
    ),
    (
        "open_file",
        "Open a file with the OS default app. If target is a folder and filename is empty, "
        "opens the first HTML file in that folder.",
        {"target": {"type": "string"}, "filename": {"type": "string"}},
        ["target"],
        lambda target, filename="": actions.open_file(target, filename or ""),
    ),
    (
        "open_folder",
        "Open a resolved folder in Explorer.",
        {"target": {"type": "string"}},
        ["target"],
        lambda target: actions.open_folder(target),
    ),
    (
        "open_url",
        "Open an http or https URL in the default browser.",
        {"url": {"type": "string"}},
        ["url"],
        lambda url: actions.open_url(url),
    ),
    (
        "read_file",
        "Read a small text file under allowed roots. Refuses binary and oversized files.",
        {"target": {"type": "string"}, "max_bytes": {"type": "integer"}},
        ["target"],
        lambda target, max_bytes=20000: actions.read_file(target, max_bytes or 20000),
    ),
    (
        "write_file",
        "Create a small UTF-8 text file under allowed roots. Refuses overwrite unless overwrite is true.",
        {"target": {"type": "string"}, "content": {"type": "string"}, "overwrite": {"type": "boolean"}},
        ["target", "content"],
        lambda target, content, overwrite=False: actions.write_file(target, content, bool(overwrite)),
    ),
    (
        "run_command",
        "Run a non-destructive shell command inside an allowed folder.",
        {"command": {"type": "string"}, "cwd": {"type": "string"}, "timeout": {"type": "integer"}},
        ["command"],
        lambda command, cwd="", timeout=120: actions.run_command(command, cwd or "", timeout or 120),
    ),
    (
        "run_tests",
        "Run tests in an allowed folder, auto-detecting a basic test command unless one is provided.",
        {"cwd": {"type": "string"}, "command": {"type": "string"}},
        [],
        lambda cwd="", command="": actions.run_tests(cwd or "", command or ""),
    ),
    (
        "codex_task",
        "Run a non-interactive Codex task in a resolved folder. Use this when the user "
        "asks Codex to create, edit, or inspect files in a folder.",
        {"target": {"type": "string"}, "instruction": {"type": "string"}},
        ["target", "instruction"],
        lambda target, instruction: actions.codex_task(target, instruction),
    ),
    (
        "open_project",
        "Open a project in an interactive Claude or Codex session in a new terminal tab. "
        "'name' is the project/folder name to resolve; 'agent' is 'claude' or 'codex'.",
        {"name": {"type": "string"}, "agent": {"type": "string", "enum": ["claude", "codex"]}},
        ["name"],
        lambda name, agent="claude": actions.open_project(name, agent or "claude"),
    ),
    (
        "remember_note",
        "Write a durable memory note to the project's Obsidian vault.",
        {"text": {"type": "string"}},
        ["text"],
        lambda text: jarvis.remember_note(text, "voice companion"),
    ),
    (
        "search_memory",
        "Search the project's long-term memory (vault) for context on a question about "
        "the project, its history, or a past decision.",
        {"query": {"type": "string"}},
        ["query"],
        _search_memory,
    ),
    (
        "web_search",
        "Search the public web and return source links.",
        {"query": {"type": "string"}},
        ["query"],
        lambda query: jarvis.web_search(query),
    ),
    (
        "web_fetch",
        "Fetch a URL and return readable page text plus the source URL.",
        {"url": {"type": "string"}},
        ["url"],
        lambda url: jarvis.web_fetch(url),
    ),
]


def build_tools(emit_event=None) -> list[FunctionSchema]:
    return [
        FunctionSchema(
            name=name,
            description=description,
            properties=properties,
            required=required,
            handler=_make_handler(name, sync_fn, emit_event),
        )
        for name, description, properties, required, sync_fn in TOOL_SPECS
    ]


# ---- failover LLM service ----------------------------------------------------------


class FailoverLLMService(OpenAILLMService):
    """Provider-agnostic primary; on the first API error, switches to the fallback
    provider for the rest of the session (sticky — avoids retrying a dead/quota-
    exhausted provider every turn). Works for either Groq-primary/OpenRouter-fallback
    or the reverse, selected in build_llm() via AI_CTO_VOICE_BRAIN."""

    def __init__(
        self,
        *,
        primary_name: str,
        api_key: str,
        base_url: str,
        model: str,
        fallback_name: str | None = None,
        fallback_api_key: str | None = None,
        fallback_base_url: str | None = None,
        fallback_model: str | None = None,
        on_failover=None,
        **kwargs,
    ):
        super().__init__(
            api_key=api_key, base_url=base_url, settings=OpenAILLMService.Settings(model=model), **kwargs
        )
        self._primary_name = primary_name
        self._fallback_name = fallback_name
        self._fallback_api_key = fallback_api_key
        self._fallback_base_url = fallback_base_url
        self._fallback_model = fallback_model
        self._fallback_client = None
        self._using_fallback = False
        self._on_failover = on_failover

    def _ensure_fallback_client(self):
        if self._fallback_client is None and self._fallback_api_key:
            from openai import AsyncOpenAI

            self._fallback_client = AsyncOpenAI(
                api_key=self._fallback_api_key, base_url=self._fallback_base_url
            )
        return self._fallback_client

    async def _completions_via(self, client, model: str, context):
        adapter = self.get_llm_adapter()
        params_from_context = adapter.get_llm_invocation_params(
            context,
            system_instruction=self._settings.system_instruction,
            convert_developer_to_user=not self.supports_developer_role,
        )
        params = self.build_chat_completion_params(params_from_context)
        params["model"] = model
        return await client.chat.completions.create(**params)

    async def get_chat_completions(self, context):
        if self._using_fallback:
            client = self._ensure_fallback_client()
            if client:
                return await self._completions_via(client, self._fallback_model, context)
        try:
            return await super().get_chat_completions(context)
        except Exception as e:
            client = self._ensure_fallback_client()
            if not client:
                raise
            logger.warning(
                f"{self._primary_name} call failed ({e}); switching to "
                f"{self._fallback_name} for the rest of this session"
            )
            self._using_fallback = True
            if self._on_failover:
                await self._on_failover(str(e))
            return await self._completions_via(client, self._fallback_model, context)


def build_llm(*, on_failover=None) -> FailoverLLMService:
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    primary = os.environ.get("AI_CTO_VOICE_BRAIN", "groq").strip().lower()
    groq_model = os.environ.get("AI_CTO_GROQ_MODEL", DEFAULT_GROQ_MODEL).strip() or DEFAULT_GROQ_MODEL
    openrouter_model = (
        os.environ.get("AI_CTO_OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL).strip()
        or DEFAULT_OPENROUTER_MODEL
    )

    if primary == "openrouter":
        if not openrouter_key:
            raise RuntimeError("AI_CTO_VOICE_BRAIN=openrouter but OPENROUTER_API_KEY is not set")
        logger.info(
            f"voice brain: OpenRouter {openrouter_model} primary"
            + (f", Groq {groq_model} fallback" if groq_key else " (no fallback key set)")
        )
        return FailoverLLMService(
            primary_name="OpenRouter",
            api_key=openrouter_key,
            base_url=OPENROUTER_BASE_URL,
            model=openrouter_model,
            fallback_name="Groq",
            fallback_api_key=groq_key or None,
            fallback_base_url=GROQ_BASE_URL,
            fallback_model=groq_model,
            on_failover=on_failover,
        )

    if not groq_key:
        raise RuntimeError(
            "GROQ_API_KEY is required for the voice brain "
            "(or set AI_CTO_VOICE_BRAIN=openrouter with OPENROUTER_API_KEY)"
        )
    logger.info(
        f"voice brain: Groq {groq_model} primary"
        + (f", OpenRouter {openrouter_model} fallback" if openrouter_key else " (no fallback key set)")
    )
    return FailoverLLMService(
        primary_name="Groq",
        api_key=groq_key,
        base_url=GROQ_BASE_URL,
        model=groq_model,
        fallback_name="OpenRouter",
        fallback_api_key=openrouter_key or None,
        fallback_base_url=OPENROUTER_BASE_URL,
        fallback_model=openrouter_model,
        on_failover=on_failover,
    )


# ---- UI events + auto-memory observer ----------------------------------------------
#
# Two small taps sharing one ObserverState, mirroring turn_timer.py's pattern: one
# sits right after STT (sees TranscriptionFrame, i.e. the user's turn), the other
# right after the LLM (sees LLMFullResponseStart/Text/EndFrame, i.e. the assistant's
# turn). Together they replace the UI-event and AutoMemory.record() duties that used
# to live inline in brain.ClaudeCTOBrain._respond.

EventCallback = Callable[[dict], Awaitable[None]]


class ObserverState:
    def __init__(self, *, events: EventCallback | None, auto_memory: bool = True):
        self.events = events
        self.memory = AutoMemory(enabled=auto_memory)
        self.last_user_text = ""
        self.answer_parts: list[str] = []

    async def emit(self, event: dict) -> None:
        if self.events:
            try:
                await self.events(event)
            except Exception as e:  # UI must never break the voice loop
                logger.warning(f"UI event callback failed: {e}")

    async def aclose(self) -> None:
        await self.memory.aclose()


class _UserTap(FrameProcessor):
    def __init__(self, state: ObserverState, **kwargs):
        super().__init__(**kwargs)
        self._state = state

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, (UserStartedSpeakingFrame, VADUserStartedSpeakingFrame)):
            await self._state.emit({"type": "user_speech_start"})
        elif isinstance(frame, (UserStoppedSpeakingFrame, VADUserStoppedSpeakingFrame)):
            await self._state.emit({"type": "user_speech_stop"})
        elif isinstance(frame, InterimTranscriptionFrame) and frame.text.strip():
            await self._state.emit({"type": "user_transcript_partial", "text": frame.text.strip()})
        elif isinstance(frame, TranscriptionFrame) and frame.text.strip():
            self._state.last_user_text = frame.text.strip()
            await self._state.emit({"type": "user_transcript", "text": self._state.last_user_text})
        await self.push_frame(frame, direction)


class _AssistantTap(FrameProcessor):
    def __init__(self, state: ObserverState, **kwargs):
        super().__init__(**kwargs)
        self._state = state

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            self._state.answer_parts = []
            await self._state.emit({"type": "bot_text_start"})
        elif isinstance(frame, LLMTextFrame) and frame.text:
            self._state.answer_parts.append(frame.text)
            await self._state.emit({"type": "bot_text", "text": frame.text})
        elif isinstance(frame, LLMFullResponseEndFrame):
            answer = "".join(self._state.answer_parts)
            if self._state.last_user_text:
                self._state.memory.record(self._state.last_user_text, answer)
            await self._state.emit({"type": "bot_text_end"})
        await self.push_frame(frame, direction)

    async def cleanup(self):
        await super().cleanup()
        await self._state.aclose()


def build_observer(events: EventCallback | None) -> tuple[FrameProcessor, FrameProcessor, ObserverState]:
    """Return (user_tap, assistant_tap, state). Place user_tap right after STT (before
    the user context aggregator) and assistant_tap right after the LLM (before TTS)."""
    state = ObserverState(events=events)
    return _UserTap(state), _AssistantTap(state), state
