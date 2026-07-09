"""memory_observer.py - automatic long-term memory from voice conversation.

Phase 2 of the Jarvis V2 upgrade (vault/research/jarvis-v2-upgrade-research.md);
brain moved to Groq in the Groq/OpenRouter voice-brain switch (see
vault/decisions/voice-brain.md) so this no longer depends on Claude quota.

After each spoken exchange the observer buffers (user, assistant) turns and, once a
few have accumulated (or the conversation goes idle), runs a cheap background pass
that distills the batch into durable facts. Default brain is **Groq** (a plain
OpenAI-compatible chat completion, free tier); set AI_CTO_MEMORY_BRAIN=haiku to use
a Claude Code Haiku session (subscription) instead. Anything worth keeping is written
to the Basic Memory vault via jarvis.remember_note, so the vault stays the single
source of truth — no mem0, no extra vector DB (see the research note for why mem0 was
deferred).

The pass is fire-and-forget: it never blocks the voice loop, dedupes within a session,
and silently no-ops if the selected brain is unavailable (no key / CLI missing).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import time
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import jarvis  # Basic Memory write helper

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_GROQ_MODEL = "llama-3.1-8b-instant"  # small/fast is plenty for fact extraction

# Tunables (env-overridable).
BATCH_TURNS = int(os.environ.get("AI_CTO_MEMORY_BATCH_TURNS", "4"))
IDLE_FLUSH_SECONDS = float(os.environ.get("AI_CTO_MEMORY_IDLE_SECONDS", "45"))
MIN_FACT_LEN = 8
MEMORY_BRAIN = os.environ.get("AI_CTO_MEMORY_BRAIN", "groq").strip().lower()

EXTRACT_SYSTEM = """You extract durable, long-term memory from a spoken conversation
between a user and their AI CTO companion.

Return ONLY facts worth remembering for weeks or months: the user's stable
preferences, decisions they made, project constraints or goals, personal facts, and
commitments. IGNORE small talk, transient status, questions, and anything already
obvious from the project. Do not invent or infer beyond what was said.

Output format: one fact per line, each a terse third-person statement (no bullets, no
numbering, max ~15 words). If there is nothing durable, output exactly the single word
NONE."""


class AutoMemory:
    """Buffers exchanges and extracts durable facts on a background pass (Groq by
    default, or a Haiku Claude Code session with AI_CTO_MEMORY_BRAIN=haiku)."""

    def __init__(self, *, enabled: bool = True):
        self._enabled = enabled and os.environ.get("AI_CTO_AUTO_MEMORY", "1") != "0"
        self._brain = MEMORY_BRAIN
        self._buffer: list[tuple[str, str]] = []
        self._seen: set[str] = set()  # de-dupe fact hashes for this session
        self._client = None  # AsyncOpenAI (groq) or ClaudeSDKClient (haiku)
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None
        self._idle_handle: asyncio.TimerHandle | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def record(self, user_text: str, assistant_text: str) -> None:
        """Note one completed exchange. Non-blocking; safe to call from the voice loop."""
        if not self._enabled:
            return
        user_text = (user_text or "").strip()
        assistant_text = (assistant_text or "").strip()
        if not user_text:
            return
        self._buffer.append((user_text, assistant_text))
        self._loop = asyncio.get_event_loop()
        self._arm_idle_timer()
        if len(self._buffer) >= BATCH_TURNS:
            self._schedule_flush()

    def _arm_idle_timer(self) -> None:
        if self._idle_handle:
            self._idle_handle.cancel()
        if self._loop:
            self._idle_handle = self._loop.call_later(IDLE_FLUSH_SECONDS, self._schedule_flush)

    def _schedule_flush(self) -> None:
        if self._flush_task and not self._flush_task.done():
            return  # a flush is already running; new turns go in the next batch
        if not self._buffer:
            return
        self._flush_task = asyncio.ensure_future(self._flush())

    async def _flush(self) -> None:
        async with self._lock:
            if not self._buffer:
                return
            batch, self._buffer = self._buffer, []
        transcript = "\n".join(f"User: {u}\nCTO: {a}" for u, a in batch)
        try:
            facts = await self._extract(transcript)
        except Exception as e:  # extraction must never break the voice session
            logger.warning(f"auto-memory extraction failed: {e}")
            return
        for fact in facts:
            await asyncio.to_thread(self._save, fact)

    async def _extract(self, transcript: str) -> list[str]:
        prompt = f"Conversation:\n{transcript}\n\nDurable facts:"
        if self._brain == "haiku":
            raw = await self._extract_haiku(prompt)
        else:
            raw = await self._extract_groq(prompt)
        if not raw or raw.strip().upper() == "NONE":
            return []
        facts: list[str] = []
        for line in raw.splitlines():
            fact = line.strip().lstrip("-*0123456789. ").strip()
            if len(fact) >= MIN_FACT_LEN and fact.upper() != "NONE":
                facts.append(fact)
        return facts

    async def _extract_groq(self, prompt: str) -> str:
        client = await self._ensure_groq_client()
        model = os.environ.get("AI_CTO_MEMORY_GROQ_MODEL", DEFAULT_GROQ_MODEL).strip() or DEFAULT_GROQ_MODEL
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            stream=False,
        )
        return (response.choices[0].message.content or "").strip()

    async def _ensure_groq_client(self):
        if self._client is None:
            api_key = os.environ.get("GROQ_API_KEY", "").strip()
            if not api_key:
                raise RuntimeError("GROQ_API_KEY not set; auto-memory (groq) is a no-op")
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=api_key, base_url=GROQ_BASE_URL)
            logger.info("auto-memory: Groq extraction client ready")
        return self._client

    async def _extract_haiku(self, prompt: str) -> str:
        from claude_agent_sdk import AssistantMessage, TextBlock

        client = await self._ensure_haiku_client()
        await client.query(prompt)
        text_parts: list[str] = []
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text:
                        text_parts.append(block.text)
        return "".join(text_parts).strip()

    async def _ensure_haiku_client(self):
        if self._client is None:
            from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

            options = ClaudeAgentOptions(
                system_prompt=EXTRACT_SYSTEM,
                model="haiku",  # cheap/fast; goes through the Claude Code subscription
                max_turns=1,
                allowed_tools=[],
                disallowed_tools=["Bash", "Write", "Edit", "Read", "WebSearch", "WebFetch", "Task"],
            )
            client = ClaudeSDKClient(options=options)
            await client.connect()
            self._client = client
            logger.info("auto-memory: Haiku extraction session connected")
        return self._client

    def _save(self, fact: str) -> None:
        digest = hashlib.sha1(fact.lower().encode("utf-8")).hexdigest()
        if digest in self._seen:
            return
        self._seen.add(digest)
        body = (
            "Auto-captured from a voice conversation.\n\n"
            f"- **Fact:** {fact}\n"
            f"- **Captured:** {time.strftime('%Y-%m-%d %H:%M')}\n"
        )
        result = jarvis.remember_note(body, source="auto-memory")
        if result.get("ok"):
            logger.info(f"auto-memory saved: {fact}")
        else:
            logger.warning(f"auto-memory write failed: {result.get('error')}")

    async def aclose(self) -> None:
        if self._idle_handle:
            self._idle_handle.cancel()
        if self._flush_task and not self._flush_task.done():
            try:
                await self._flush_task
            except Exception:
                pass
        # Final drain of anything still buffered.
        if self._buffer:
            try:
                await self._flush()
            except Exception:
                pass
        if self._client:
            try:
                # AsyncOpenAI (groq) uses close(); ClaudeSDKClient (haiku) uses disconnect().
                closer = getattr(self._client, "close", None) or getattr(self._client, "disconnect", None)
                if closer:
                    await closer()
            except Exception:
                pass
            self._client = None
