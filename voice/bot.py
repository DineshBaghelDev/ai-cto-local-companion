"""bot.py - voice companion server.

Pipeline (see vault/decisions/voice-brain.md):
  browser mic --SmallWebRTC--> Silero VAD -> faster-whisper STT (GPU turbo when
  CUDA is available; see services.py) -> user aggregator (Smart Turn v3 stop
  strategy) -> voice_brain (Groq primary, OpenRouter fallback; native tool
  calling for memory/escalation/coding-dispatch/web) -> Kokoro TTS (ONNX)
  --SmallWebRTC--> browser speaker

Also serves:
  /                    the UI (static/index.html)
  POST /api/offer      SmallWebRTC signaling
  WS   /ws/events      transcript + memory-used events for the UI
  POST /api/save_memory  writes the last exchange to the vault via basic-memory CLI

Run:  .venv\\Scripts\\python bot.py   ->  http://localhost:7860
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

# aiortc datagram-size workaround: oversized SCTP chunks get dropped by the kernel
# (EMSGSIZE) and the data channel stalls; 1100 keeps datagrams under any sane MTU.
os.environ.setdefault("PIPECAT_SCTP_MAX_CHUNK_SIZE", "1100")

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from loguru import logger

from pipecat.frames.frames import TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

import services
import turn_timer
import voice_brain
import escalation  # scripts/escalation.py (sys.path set up by voice_brain import)
import coder
import jarvis
import supervisor

VOICE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = VOICE_DIR.parent
AUDIO_PREFS = PROJECT_ROOT / ".run" / "audio-devices.json"

app = FastAPI()
webrtc_handler = SmallWebRTCRequestHandler()
STARTED_AT = time.time()

# ---- UI event fan-out --------------------------------------------------------------

ui_sockets: set[WebSocket] = set()


async def emit_ui(event: dict) -> None:
    dead = []
    for ws in ui_sockets:
        try:
            await ws.send_text(json.dumps(event))
        except Exception:
            dead.append(ws)
    for ws in dead:
        ui_sockets.discard(ws)


@app.websocket("/ws/events")
async def ws_events(ws: WebSocket):
    await ws.accept()
    ui_sockets.add(ws)
    try:
        while True:
            await ws.receive_text()  # UI doesn't send anything; keepalive only
    except WebSocketDisconnect:
        ui_sockets.discard(ws)


@app.post("/api/events")
async def receive_event(event: dict):
    await emit_ui(event)
    return {"ok": True}


# ---- the pipecat pipeline ----------------------------------------------------------


async def run_bot(connection: SmallWebRTCConnection) -> None:
    transport = SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    )

    stt = services.build_stt()
    tts = services.build_tts()

    async def on_failover(reason: str) -> None:
        await emit_ui({"type": "failover", "text": f"Groq unavailable, switched to OpenRouter: {reason}"})

    llm = voice_brain.build_llm(on_failover=on_failover)
    tools = voice_brain.build_tools(emit_event=emit_ui)
    context = LLMContext(messages=[{"role": "system", "content": voice_brain.SYSTEM_PROMPT}], tools=tools)
    user_tap, assistant_tap, _obs_state = voice_brain.build_observer(emit_ui)
    aggregators = LLMContextAggregatorPair(
        context,
        # Browser sessions are explicit (user opened the page), so no wake gate;
        # Smart Turn v3 stop strategy stays the default.
        user_params=services.build_user_params(wake=False),
    )

    stt_tap, llm_tap, tts_tap = turn_timer.build_taps()
    stages = [transport.input(), stt]
    if stt_tap is not None:
        stages.append(stt_tap)
    stages += [user_tap, aggregators.user(), llm, assistant_tap]
    if llm_tap is not None:
        stages.append(llm_tap)
    stages.append(tts)
    if tts_tap is not None:
        stages.append(tts_tap)
    stages += [transport.output(), aggregators.assistant()]
    pipeline = Pipeline(stages)
    task = PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=True),
        idle_timeout_secs=None,
    )

    @transport.event_handler("on_client_connected")
    async def on_connect(transport, client):
        pending = await asyncio.to_thread(escalation.list_blockers, False)
        if pending:
            b = pending[0]
            await task.queue_frames([TTSSpeakFrame(
                f"Voice link up. Heads up: {len(pending)} coding task"
                f"{'s are' if len(pending) > 1 else ' is'} blocked and waiting on your "
                f"decision — the first is {b['task_text']}. Ask me about the blocker "
                f"when you're ready.")])
        else:
            await task.queue_frames([TTSSpeakFrame("Voice link up.")])

    @transport.event_handler("on_client_disconnected")
    async def on_disconnect(transport, client):
        logger.info("client disconnected; stopping pipeline")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)


# ---- HTTP endpoints ----------------------------------------------------------------


@app.get("/")
async def index():
    return FileResponse(VOICE_DIR / "static" / "index.html")


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.post("/api/offer")
async def offer(request: dict):
    answer: dict | None = None

    async def on_connection(connection: SmallWebRTCConnection) -> None:
        asyncio.create_task(run_bot(connection))

    answer = await webrtc_handler.handle_web_request(
        request=SmallWebRTCRequest.from_dict(request),
        webrtc_connection_callback=on_connection,
    )
    return answer


def _codex_exe() -> str:
    exe = shutil.which("codex")
    if not exe:
        raise RuntimeError("codex CLI not found on PATH")
    return exe


def _prompt(message: str) -> str:
    return f"""You are the fast text brain for a local AI CTO companion.

Rules:
- Use this tiny local context; do not pretend you read more than this.
- Do not push, merge, deploy, or use API keys.
- Coding execution is handled by local Jarvis tools before this prompt reaches you.
- Keep answers concise and practical.

Project root: {PROJECT_ROOT}
Memory vault: {PROJECT_ROOT / "vault"}
Tiny context:
{jarvis.tiny_memory_context(message)}

User message:
{message}
"""


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _clean_external_output(text: str) -> str:
    text = _ANSI_RE.sub("", text or "").replace("\r", "\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "(no response)"
    assistant_lines = [line for line in lines if line.startswith("Assistant:")]
    if assistant_lines:
        return assistant_lines[-1].removeprefix("Assistant:").strip() or "(no response)"
    return "\n".join(lines[-20:]).strip() or "(no response)"


def _external_chat_candidates() -> list[tuple[list[str], str]]:
    if os.environ.get("CHATGPT_SESSION_TOKEN") and shutil.which("chatgpt-cli"):
        return [(["chatgpt-cli"], "chatgpt-cli")]
    candidates: list[tuple[list[str], str]] = []
    if shutil.which("tgpt"):
        candidates.append((["tgpt"], "tgpt"))
    if os.environ.get("CHATGPT_SESSION_TOKEN") and shutil.which("chatgpt"):
        candidates.append((["chatgpt"], "chatgpt"))
    return candidates


def _external_chat(message: str) -> tuple[str, str] | None:
    cmd = os.environ.get("AI_CTO_TEXT_BRAIN_CMD")
    candidates: list[tuple[list[str], str]]
    if cmd:
        argv = shlex.split(cmd, posix=os.name != "nt")
        tool_name = Path(argv[0]).stem if argv else "custom"
        candidates = [(argv, tool_name)]
    else:
        candidates = _external_chat_candidates()
        if not candidates:
            return None
    prompt = _prompt(message)
    use_stdin = os.environ.get("AI_CTO_TEXT_BRAIN_STDIN") == "1"
    env = os.environ.copy()
    if env.get("CHATGPT_SESSION_TOKEN") and not env.get("TOKEN"):
        env["TOKEN"] = env["CHATGPT_SESSION_TOKEN"]
    errors: list[str] = []
    for argv, name in candidates:
        try:
            proc = subprocess.run(
                argv if use_stdin else [*argv, prompt],
                input=prompt if use_stdin else "",
                cwd=PROJECT_ROOT, env=env, text=True, capture_output=True,
                encoding="utf-8", errors="replace", timeout=90,
            )
        except subprocess.TimeoutExpired as e:
            partial = _clean_external_output((e.stdout or "") + "\n" + (e.stderr or ""))
            if partial != "(no response)":
                return partial, name
            errors.append(f"{name} timed out")
            continue
        if proc.returncode == 0:
            return _clean_external_output(proc.stdout), name
        errors.append((proc.stderr or proc.stdout or f"{name} failed")[-400:])
    raise RuntimeError("; ".join(errors)[-1200:])


def _local_chat(message: str) -> str | None:
    q = message.strip().lower()
    remember = jarvis.remember_text(message)
    if remember:
        saved = jarvis.remember_note(remember, "text companion")
        return f"Remembered: {saved['title']}" if saved["ok"] else f"Memory write failed: {saved['error']}"
    if q in {"help", "commands", "what can you do"}:
        return ("I can remember notes, search memory, use simple web search/fetch, start coding tasks "
                "when a default repo is configured, and report current task status.")
    if any(word in q for word in ("status", "progress", "working on", "in progress", "what's going on", "whats going on")):
        return _format_status(coder.status_snapshot())
    if "github" in q or " gh" in q or "gh " in q:
        return "GitHub CLI is available through WSL gh; PR and merge commands are explicit, not automatic."
    if "blocker" in q:
        rows = escalation.list_blockers(True)
        if not rows:
            return "No blockers are recorded right now."
        return "\n".join(f"{r['status']}: {r['task_id']} - {r['task_text']}" for r in rows[:5])
    if q.startswith("search "):
        term = message.strip()[7:].strip()
        if not term:
            return "Give me a term after 'search'."
        rg = shutil.which("rg")
        if not rg:
            return "ripgrep is not on PATH, so vault search is unavailable."
        proc = subprocess.run([rg, "-n", "-i", "--glob", "*.md", term, str(PROJECT_ROOT / "vault")],
                              text=True, capture_output=True, encoding="utf-8", errors="replace",
                              timeout=20)
        hits = (proc.stdout or "").strip().splitlines()[:8]
        return "\n".join(hits) if hits else f"No vault hits for {term!r}."
    url = jarvis.first_url(message)
    if url:
        page = jarvis.web_fetch(url)
        return f"Fetched {page['url']}\n\n{page['text'][:1800] or '(no readable text)'}"
    if any(w in q for w in ("internet", "web", "online", "latest", "current", "today")):
        query = re.sub(r"\b(search|the|web|internet|online|latest|current|today|for)\b", " ", message, flags=re.I).strip()
        result = jarvis.web_search(query or message)
        links = "\n".join(result["results"]) or "(no links found)"
        return f"Web search: {result['query']}\n{links}\nSource: {result['source']}"
    if jarvis.looks_like_task(message):
        repo = os.environ.get("AI_CTO_DEFAULT_REPO")
        if not repo:
            result = coder.start_task(None, "Voice requested task", message)
            return result["error"]
        title = re.sub(r"\s+", " ", message).strip()[:60] or "Voice requested task"
        asyncio.create_task(asyncio.to_thread(coder.start_task, repo, title, message, "claude"))
        return f"Understood, I'll start working on: {message}. I'll log progress so you can ask me what's going on."
    return None


def _format_status(snap: dict) -> str:
    if snap["active"]:
        lines = ["Current work:"]
        lines += [f"{t['status']}: {t['task_id']} - {t['text']}" for t in snap["active"][:5]]
        return "\n".join(lines)
    if snap["tasks"]:
        last = snap["tasks"][0]
        return f"No active task right now. Latest run is {last['run_id']}; latest task is {last['status']}: {last['text']}."
    acts = snap["activity"]
    if acts:
        return "No active task right now. Latest activity: " + acts[0]["message"]
    return "No active task right now."


def _status_snapshot() -> dict:
    snap = coder.status_snapshot()
    sup = supervisor.current_run_snapshot()
    snap["supervisor"] = sup
    for step in sup.get("steps", [])[:5]:
        snap.setdefault("activity", []).insert(0, {
            "ts": step.get("completed_at") or step.get("started_at") or "",
            "kind": "tool:" + step.get("status", ""),
            "message": f"{step.get('tool')}: {step.get('output_summary') or step.get('proof') or ''}",
        })
    return snap


def _codex_chat(message: str) -> str:
    prompt = _prompt(message)
    out = PROJECT_ROOT / ".run" / "codex-last.txt"
    out.parent.mkdir(exist_ok=True)
    proc = subprocess.run([
        _codex_exe(), "exec",
        "--sandbox", "read-only",
        "--cd", str(PROJECT_ROOT),
        "--skip-git-repo-check",
        "--output-last-message", str(out),
        prompt,
    ], cwd=PROJECT_ROOT, text=True, capture_output=True,
       encoding="utf-8", errors="replace", timeout=180)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "codex failed")[-1200:])
    if out.exists():
        text = out.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            return text
    return (proc.stdout or "").strip() or "(no response)"


@app.post("/api/chat")
async def chat(payload: dict):
    message = (payload.get("message") or "").strip()
    if not message:
        return {"ok": False, "error": "empty message"}
    try:
        local = _local_chat(message)
        if local is not None:
            return {"ok": True, "reply": local, "source": "local"}
        external = await asyncio.to_thread(_external_chat, message)
        if external is not None:
            reply, source = external
            return {"ok": True, "reply": reply, "source": source}
        reply = await asyncio.to_thread(_codex_chat, message)
        return {"ok": True, "reply": reply, "source": "codex"}
    except Exception as e:
        logger.error(f"codex chat failed: {e}")
        return {"ok": False, "error": str(e)}


@app.get("/api/status")
async def status():
    return await asyncio.to_thread(_status_snapshot)


@app.get("/api/runtime")
async def runtime():
    return await asyncio.to_thread(_runtime_snapshot)


@app.get("/api/audio/devices")
async def audio_devices():
    return await asyncio.to_thread(_audio_devices)


@app.post("/api/audio/restart")
async def audio_restart(payload: dict):
    input_device = int(payload.get("input_device"))
    output_device = int(payload.get("output_device"))
    return await asyncio.to_thread(_restart_desktop_voice, input_device, output_device)


def _runtime_snapshot() -> dict:
    desktop_log = PROJECT_ROOT / "logs" / "desktop-voice.log"
    voice_log = PROJECT_ROOT / "logs" / "voice.log"
    devices = _last_device_lines(desktop_log)
    last_error = _last_matching_line(desktop_log, "ERROR")
    ready = _last_event_is_ready(desktop_log)
    return {
        "ok": True,
        "pid": os.getpid(),
        "uptime_seconds": int(time.time() - STARTED_AT),
        "default_repo": os.environ.get("AI_CTO_DEFAULT_REPO") or "",
        "whisper_model": services.stt_description(),
        "tts": services.tts_description(),
        "wake_engine": services.wake_engine(),
        "desktop_voice": {
            "ready": ready,
            "input": devices.get("input", ""),
            "output": devices.get("output", ""),
            "last_error": "" if ready else last_error,
            "last_user": _last_matching_line(desktop_log, "you:"),
            "last_cto": _last_matching_line(desktop_log, "cto:"),
        },
        "server": {
            "ready": _log_has(voice_log, "Uvicorn running"),
            "last_error": _last_matching_line(voice_log, "ERROR"),
        },
    }


def _tail(path: Path, max_chars: int = 20000) -> str:
    text = ""
    for p in (path, path.with_name(path.name.replace(".log", ".err.log"))):
        if not p.exists():
            continue
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_chars))
            text += f.read().decode("utf-8", errors="replace")
    return text


def _log_has(path: Path, needle: str) -> bool:
    return needle in _tail(path)


def _last_matching_line(path: Path, needle: str) -> str:
    for line in reversed(_tail(path).splitlines()):
        if needle in line:
            return line.strip()[-500:]
    return ""


def _last_event_is_ready(path: Path) -> bool:
    for line in reversed(_tail(path).splitlines()):
        if "Idle timeout detected" in line or "has finished" in line:
            return False
        if "Desktop voice is ready" in line:
            return True
        if "ERROR" in line or "Traceback" in line:
            return False
    return False


def _last_device_lines(path: Path) -> dict:
    out = {"input": "", "output": ""}
    for line in _tail(path).splitlines():
        if "audio input:" in line:
            out["input"] = line.split("audio input:", 1)[-1].strip()
        elif "audio output:" in line:
            out["output"] = line.split("audio output:", 1)[-1].strip()
    return out


def _audio_devices() -> dict:
    import pyaudio

    pa = pyaudio.PyAudio()
    try:
        devices = []
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            devices.append({
                "id": i,
                "name": d["name"],
                "inputs": int(d["maxInputChannels"]),
                "outputs": int(d["maxOutputChannels"]),
            })
    finally:
        pa.terminate()
    prefs = _load_audio_prefs()
    return {"devices": devices, "prefs": prefs}


def _load_audio_prefs() -> dict:
    if AUDIO_PREFS.exists():
        try:
            return json.loads(AUDIO_PREFS.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"input_device": 3, "output_device": 5}


def _save_audio_prefs(input_device: int, output_device: int) -> None:
    AUDIO_PREFS.parent.mkdir(exist_ok=True)
    AUDIO_PREFS.write_text(json.dumps({
        "input_device": input_device,
        "output_device": output_device,
    }), encoding="utf-8")


def _restart_desktop_voice(input_device: int, output_device: int) -> dict:
    devices = _audio_devices()["devices"]
    by_id = {d["id"]: d for d in devices}
    if input_device not in by_id or by_id[input_device]["inputs"] < 1:
        return {"ok": False, "error": f"device {input_device} is not an input device"}
    if output_device not in by_id or by_id[output_device]["outputs"] < 1:
        return {"ok": False, "error": f"device {output_device} is not an output device"}
    if "camo" in by_id[input_device]["name"].lower():
        return {"ok": False, "error": "Camo is blocked as an input device"}

    _save_audio_prefs(input_device, output_device)
    ps = r"""
Get-CimInstance Win32_Process |
  Where-Object {
    $cmd = $_.CommandLine
    $cmd -and $_.Name -match "^(cmd|python|pythonw)\.exe$" -and
      ($cmd -match "desktop_voice.py" -or $cmd -match "\\.run\\desktop-voice.cmd")
  } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
"""
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   capture_output=True, text=True, timeout=20)
    cmd = PROJECT_ROOT / ".run" / "desktop-voice.cmd"
    repo_line = (f'set "AI_CTO_DEFAULT_REPO={os.environ.get("AI_CTO_DEFAULT_REPO")}"'
                 if os.environ.get("AI_CTO_DEFAULT_REPO") else "rem AI_CTO_DEFAULT_REPO not set")
    cmd.write_text("\n".join([
        "@echo off",
        f'cd /d "{VOICE_DIR}"',
        repo_line,
        f'"{VOICE_DIR / ".venv" / "Scripts" / "python.exe"}" desktop_voice.py '
        f'--input-device {input_device} --output-device {output_device} '
        f'> "{PROJECT_ROOT / "logs" / "desktop-voice.log"}" 2>&1',
        "",
    ]), encoding="ascii")
    subprocess.Popen(["cmd.exe", "/c", str(cmd)], cwd=PROJECT_ROOT,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"ok": True, "input": by_id[input_device], "output": by_id[output_device]}


@app.get("/api/blockers")
async def blockers():
    """Dashboard feed: all blockers (pending first is the UI's job)."""
    return await asyncio.to_thread(escalation.list_blockers, True)


@app.post("/api/save_memory")
async def save_memory(payload: dict):
    """Write the last user/assistant exchange to the vault (voice-notes/)."""
    user = (payload.get("user") or "").strip()
    assistant = (payload.get("assistant") or "").strip()
    if not (user or assistant):
        return {"ok": False, "error": "nothing to save"}

    now = datetime.datetime.now()
    title = f"Voice note {now:%Y-%m-%d %H:%M}"
    body = (
        f"# {title}\n\n- **Date:** {now:%Y-%m-%d %H:%M}\n- **Source:** voice companion\n\n"
        f"## Human\n\n{user or '(voice input not captured)'}\n\n"
        f"## CTO\n\n{assistant or '(no answer)'}\n\n"
        f"## Relations\n- part_of [[Index]]\n"
    )
    result = await asyncio.to_thread(jarvis.remember_note, body, "voice companion")
    if not result["ok"]:
        logger.error(f"write-note failed: {result['error']}")
        return {"ok": False, "error": result["error"]}
    return {"ok": True, "title": result["title"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()
    logger.info(f"Voice companion at http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
