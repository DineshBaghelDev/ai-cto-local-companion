"""actions.py - voice-driven filesystem + agent actions (Phase 3).

Two capabilities the user asked for:
  * quick_task(target, instruction): make a quick change to a file/folder anywhere in
    the allowed roots by spawning a headless `claude -p` worker with acceptEdits in the
    target directory (Claude Code subscription, no API key).
  * open_project(name, agent): open a project in an interactive Claude or Codex session
    in a new Windows Terminal tab.

Folder/file resolution ("that folder", "the summarizer project") uses the Everything
CLI (es.exe) when present, else a bounded filesystem walk over the configured roots.

Safety: quick_task only ever runs inside an allowed root (the whole E: drive plus any
AI_CTO_PROJECT_ROOTS by default) — never system directories — and the voice brain
itself stays read-only; edits happen only in this separate, scoped subprocess.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import difflib
import webbrowser
from pathlib import Path

# Directory names never treated as project matches / never editable.
_DENY_PARTS = {"windows", "system32", "program files", "program files (x86)",
               "$recycle.bin", "appdata"}


def _search_roots() -> list[Path]:
    raw = os.environ.get("AI_CTO_PROJECT_ROOTS", "")
    roots = [Path(p.strip()) for p in raw.split(os.pathsep) if p.strip()]
    if not roots:
        roots = [Path(r"E:\Projects")]
    return [r for r in roots if r.exists()]


def _allowed_roots() -> list[Path]:
    """Roots quick_task may write inside. The whole E: drive plus configured roots."""
    roots = list(_search_roots())
    e_drive = Path("E:\\")
    if e_drive.exists():
        roots.append(e_drive)
    extra = os.environ.get("AI_CTO_ALLOWED_ROOTS", "")
    roots += [Path(p.strip()) for p in extra.split(os.pathsep) if p.strip()]
    return roots


def is_allowed(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    parts_lower = {p.lower() for p in resolved.parts}
    if parts_lower & _DENY_PARTS:
        return False
    for root in _allowed_roots():
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _everything_search(query: str, folder_only: bool) -> list[Path]:
    es = shutil.which("es")
    if not es:
        return []
    args = [es, "-n", "20"]
    if folder_only:
        args.append("/ad")  # directories only
    args.append(query)
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=15,
                              encoding="utf-8", errors="replace")
    except Exception:
        return []
    return [Path(line) for line in (proc.stdout or "").splitlines() if line.strip()]


def _walk_dirs(name: str, max_depth: int = 3) -> list[Path]:
    """Bounded case-insensitive directory search over the configured roots."""
    want = name.strip().lower()
    exact: list[Path] = []
    partial: list[Path] = []
    fuzzy: list[Path] = []
    for root in _search_roots():
        root = root.resolve()
        base_depth = len(root.parts)
        for dirpath, dirnames, _ in os.walk(root):
            depth = len(Path(dirpath).parts) - base_depth
            if depth >= max_depth:
                dirnames[:] = []
            for d in dirnames:
                low = d.lower()
                if low in _DENY_PARTS:
                    continue
                if low == want:
                    exact.append(Path(dirpath) / d)
                elif want in low or want.replace(" ", "_") in low:
                    partial.append(Path(dirpath) / d)
                elif difflib.SequenceMatcher(None, want.replace(" ", ""), low.replace("_", "")).ratio() >= 0.78:
                    fuzzy.append(Path(dirpath) / d)
    return exact + partial + fuzzy


def resolve_dir(name: str) -> Path | None:
    """Best-effort resolution of a spoken folder/project name to a directory."""
    name = (name or "").strip().strip("\"'")
    if not name:
        return None
    p = Path(name)
    if p.is_dir():
        return p
    if p.is_file():
        return p.parent
    # Everything CLI first (fast, whole-drive), then bounded walk.
    for cand in _everything_search(name, folder_only=True):
        if cand.is_dir() and is_allowed(cand):
            return cand
    hits = [h for h in _walk_dirs(name) if is_allowed(h)]
    return hits[0] if hits else None


def resolve_file(name: str, within: Path | None = None) -> Path | None:
    name = (name or "").strip().strip("\"'")
    if not name:
        return None
    p = Path(name)
    if p.is_file():
        return p
    roots = [within] if within else _search_roots()
    for root in roots:
        if not root or not root.exists():
            continue
        for match in list(root.rglob(name))[:1]:
            if match.is_file() and is_allowed(match):
                return match
    for cand in _everything_search(name, folder_only=False):
        if cand.is_file() and is_allowed(cand):
            return cand
    return None


def _proof(action: str, target: Path | str, summary: str, **extra) -> dict:
    target_s = str(target)
    return {
        "ok": True,
        "action": action,
        "target": target_s,
        "summary": summary,
        "proof": f"target={target_s}",
        **extra,
    }


def find_file(query: str, kind: str = "any", limit: int = 10) -> dict:
    """Find allowed files/folders by name."""
    query = (query or "").strip().strip("\"'")
    if not query:
        return {"ok": False, "action": "find_file", "error": "empty query", "proof": "input_checked"}
    folder_only = kind == "folder"
    hits = [p for p in _everything_search(query, folder_only=folder_only) if is_allowed(p)]
    if not hits:
        if folder_only:
            hits = [p for p in _walk_dirs(query) if is_allowed(p)]
        else:
            f = resolve_file(query)
            hits = [f] if f else []
    found = [str(p) for p in hits[: max(1, min(limit, 25))]]
    return {
        "ok": True,
        "action": "find_file",
        "target": query,
        "summary": f"Found {len(found)} match(es)",
        "proof": f"matches={len(found)}",
        "matches": found,
    }


def open_file(target: str, filename: str = "") -> dict:
    """Open a resolved file with the OS default app."""
    target = (target or "").strip().strip("\"'")
    filename = (filename or "").strip().strip("\"'")
    if filename:
        folder = resolve_dir(target)
        path = resolve_file(filename, folder)
    else:
        path = resolve_file(target)
        if path is None:
            folder = resolve_dir(target)
            if folder:
                html = sorted(folder.glob("*.html"))
                path = html[0] if html else None
    if path is None:
        return {"ok": False, "error": f"could not find file {filename or target!r}"}
    if not is_allowed(path):
        return {"ok": False, "error": f"{path} is outside the allowed roots; refusing to open"}
    try:
        os.startfile(path)  # type: ignore[attr-defined]
    except Exception as e:
        return {"ok": False, "error": f"failed to open {path}: {e}", "path": str(path)}
    return _proof("open_file", path, f"Opened {path}", path=str(path))


def open_folder(target: str) -> dict:
    folder = resolve_dir(target)
    if folder is None:
        return {"ok": False, "action": "open_folder", "error": f"could not find folder {target!r}",
                "proof": "resolution_failed"}
    if not is_allowed(folder):
        return {"ok": False, "action": "open_folder", "error": f"{folder} is outside allowed roots",
                "proof": "allowed_root_checked"}
    os.startfile(folder)  # type: ignore[attr-defined]
    return _proof("open_folder", folder, f"Opened folder {folder}", path=str(folder))


def open_url(url: str) -> dict:
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "action": "open_url", "error": "URL must start with http:// or https://",
                "proof": "scheme_checked"}
    webbrowser.open(url)
    return {"ok": True, "action": "open_url", "target": url, "summary": f"Opened {url}", "proof": "browser_open_called"}


def read_file(target: str, max_bytes: int = 20000) -> dict:
    path = resolve_file(target)
    if path is None:
        return {"ok": False, "action": "read_file", "error": f"could not find file {target!r}",
                "proof": "resolution_failed"}
    if not is_allowed(path):
        return {"ok": False, "action": "read_file", "error": f"{path} is outside allowed roots",
                "proof": "allowed_root_checked"}
    size = path.stat().st_size
    if size > max_bytes:
        return {"ok": False, "action": "read_file", "target": str(path),
                "error": f"file is {size} bytes; max is {max_bytes}", "proof": f"size={size}"}
    raw = path.read_bytes()
    if b"\x00" in raw[:2048]:
        return {"ok": False, "action": "read_file", "target": str(path),
                "error": "binary file refused", "proof": "nul_byte_detected"}
    text = raw.decode("utf-8", errors="replace")
    return _proof("read_file", path, f"Read {len(text)} characters", text=text, bytes=size)


def write_file(target: str, content: str, overwrite: bool = False) -> dict:
    path = Path((target or "").strip().strip("\"'"))
    if not path.is_absolute():
        path = (_search_roots()[0] / path) if _search_roots() else path
    path = path.resolve()
    if not is_allowed(path):
        return {"ok": False, "action": "write_file", "target": str(path),
                "error": f"{path} is outside allowed roots", "proof": "allowed_root_checked"}
    if path.exists() and not overwrite:
        return {"ok": False, "action": "write_file", "target": str(path),
                "error": "approval required: overwrite existing file", "proof": "overwrite_checked"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content or "", encoding="utf-8")
    size = path.stat().st_size
    return _proof("write_file", path, f"Wrote {size} bytes", bytes=size, proof=f"file_exists bytes={size}")


def _command_allowed(command: str, cwd: Path) -> str | None:
    low = command.lower()
    risky = ("rm ", "del ", "erase ", "rmdir ", "remove-item", "git push", "git merge",
             "gh pr merge", "npm install -g", "pip install --user", "set-executionpolicy")
    if any(x in low for x in risky):
        return "approval required: risky command"
    if not is_allowed(cwd):
        return f"{cwd} is outside allowed roots"
    return None


def run_command(command: str, cwd: str = "", timeout: int = 120) -> dict:
    workdir = resolve_dir(cwd) if cwd else Path.cwd()
    if workdir is None:
        return {"ok": False, "action": "run_command", "error": f"could not find cwd {cwd!r}",
                "proof": "cwd_resolution_failed"}
    risk = _command_allowed(command or "", workdir)
    if risk:
        return {"ok": False, "action": "run_command", "target": str(workdir), "error": risk,
                "proof": "risk_checked"}
    proc = subprocess.run(command, cwd=str(workdir), shell=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    output = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
    return {
        "ok": proc.returncode == 0,
        "action": "run_command",
        "target": str(workdir),
        "summary": (output or f"exit code {proc.returncode}")[-1500:],
        "proof": f"exit_code={proc.returncode}",
        "exit_code": proc.returncode,
    }


def run_tests(cwd: str = "", command: str = "") -> dict:
    workdir = resolve_dir(cwd) if cwd else Path.cwd()
    if workdir is None:
        return {"ok": False, "action": "run_tests", "error": f"could not find cwd {cwd!r}",
                "proof": "cwd_resolution_failed"}
    if not command:
        if (workdir / "pyproject.toml").exists() or list(workdir.glob("test_*.py")):
            command = "python -m pytest -q"
        elif (workdir / "package.json").exists():
            command = "npm test"
        else:
            command = "python -m pytest -q"
    result = run_command(command, str(workdir), timeout=180)
    result["action"] = "run_tests"
    return result


def _claude_exe() -> str:
    return shutil.which("claude") or "claude"


def _codex_exe() -> str:
    return shutil.which("codex") or "codex"


def codex_task(target: str, instruction: str, timeout: int = 300) -> dict:
    """Run a scoped non-interactive Codex task in a resolved folder."""
    instruction = (instruction or "").strip()
    if not instruction:
        return {"ok": False, "error": "no instruction given"}
    workdir = resolve_dir(target)
    if workdir is None:
        return {"ok": False, "error": f"could not find a project matching {target!r}"}
    if not is_allowed(workdir):
        return {"ok": False, "error": f"{workdir} is outside the allowed roots; refusing to edit"}

    out_file = workdir / ".codex-last.txt"
    cmd = [
        _codex_exe(), "exec",
        "--sandbox", "workspace-write",
        "--cd", str(workdir),
        "--skip-git-repo-check",
        "--output-last-message", str(out_file),
        instruction,
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=str(workdir), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Codex timed out after {timeout}s in {workdir}"}
    except FileNotFoundError:
        return {"ok": False, "error": "codex CLI not found on PATH"}

    summary = out_file.read_text(encoding="utf-8", errors="replace").strip() if out_file.exists() else ""
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout or summary or "codex failed")[-1000:],
                "dir": str(workdir)}
    return {"ok": True, "action": "codex_task", "target": str(workdir), "dir": str(workdir),
            "summary": summary[-1500:] or (proc.stdout or "").strip()[-1500:],
            "proof": f"exit_code={proc.returncode} dir={workdir}"}


def quick_task(target: str, instruction: str, timeout: int = 300) -> dict:
    """Make a quick, scoped code/file change via a headless Claude Code worker.

    `target` may be a directory, a file, or a spoken name to resolve. The worker runs
    with acceptEdits in the resolved directory and is limited to file-editing tools.
    """
    instruction = (instruction or "").strip()
    if not instruction:
        return {"ok": False, "error": "no instruction given"}

    target = (target or "").strip().strip("\"'")
    workdir: Path | None = None
    file_hint = ""
    if target:
        p = Path(target)
        if p.is_file():
            workdir, file_hint = p.parent, p.name
        elif p.is_dir():
            workdir = p
        else:
            workdir = resolve_dir(target)
            if workdir is None:
                f = resolve_file(target)
                if f is not None:
                    workdir, file_hint = f.parent, f.name
    if workdir is None:
        return {"ok": False, "error": f"could not find a folder or file matching {target!r}"}
    if not is_allowed(workdir):
        return {"ok": False, "error": f"{workdir} is outside the allowed roots; refusing to edit"}

    prompt = instruction
    if file_hint:
        prompt = f"In the file {file_hint}: {instruction}"

    cmd = [
        _claude_exe(), "-p", prompt,
        "--permission-mode", "acceptEdits",
        "--allowedTools", "Edit", "Write", "Read", "Grep", "Glob",
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=str(workdir), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"quick task timed out after {timeout}s in {workdir}"}
    except FileNotFoundError:
        return {"ok": False, "error": "claude CLI not found on PATH"}

    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or out or "claude worker failed")[-600:],
                "dir": str(workdir)}
    return {"ok": True, "action": "quick_task", "target": str(workdir), "dir": str(workdir), "file": file_hint,
            "summary": out[-1500:] or "(worker produced no text output)",
            "proof": f"exit_code={proc.returncode} dir={workdir}"}


def open_project(name: str, agent: str = "claude") -> dict:
    """Open a project in an interactive Claude or Codex session in a new terminal tab."""
    agent = (agent or "claude").strip().lower()
    if agent not in {"claude", "codex"}:
        return {"ok": False, "error": f"unknown agent {agent!r}; use claude or codex"}
    workdir = resolve_dir(name)
    if workdir is None:
        return {"ok": False, "error": f"could not find a project matching {name!r}"}

    launcher = _claude_exe() if agent == "claude" else _codex_exe()
    wt = shutil.which("wt")
    try:
        if wt:
            # New Windows Terminal tab, working dir set, agent left running interactively.
            subprocess.Popen([wt, "-w", "0", "nt", "-d", str(workdir),
                              "cmd", "/k", launcher])
        else:
            subprocess.Popen(["cmd", "/c", "start", "cmd", "/k", launcher],
                             cwd=str(workdir))
    except Exception as e:
        return {"ok": False, "error": f"failed to launch {agent}: {e}"}
    return {"ok": True, "action": "open_project", "target": str(workdir), "dir": str(workdir), "agent": agent,
            "summary": f"Opened {workdir} in {agent}", "proof": f"process_started agent={agent}"}
