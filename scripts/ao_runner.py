#!/usr/bin/env python3
"""ao_runner.py - isolated adapter for the decided coding runner.

Decision of record: decisions/coding-runner.md  ->  AgentWrapper/agent-orchestrator (AO),
independently pinned by decisions/agent-orchestrator-repo.md. This prompt builds glue only;
it does NOT re-pick the runner.

THIS FILE IS THE ONLY PLACE THAT KNOWS AO'S COMMAND SURFACE.
If the installed `ao` CLI differs (command names / flags / json shape), fix the templates in
AO_CMDS and the small parsers below - nothing else in the glue changes.

VERIFIED against @aoagents/ao@0.10.1-nightly (Ubuntu WSL2, 2026-07-06) - the headless-first
nightly, NOT stable 0.10.0. The nightly surface differs materially from 0.10.0:
    daemon  : ao status [--json]                         (orchestrator health; started by `ao start`)
    register: ao project add <abs-path>                  (no --id / --worker-agent; id is auto)
    list ids: ao project ls                              (TEXT, no --json: "<id> (<name>)")
    spawn   : ao spawn --agent <a> --prompt "<text>"     (NO --project / --branch / --json flags;
                                                           targets the DEFAULT project -> we set it)
    status  : ao session ls --json -p <projectId>        (there is NO `ao session get` in nightly)
    kill    : ao session kill <sid>
    send    : ao send <sid> <message>                    (for later escalation replies)

CONFIRMED session JSON shape (live `ao session ls --json`, root daemon, session sr-1):
    {"data":[{"id","projectId","projectName","role","branch","status","issueId","pr",
              "workspacePath","lastActivityAt"}], "meta":{...}}
  -> session id = `id`; worktree = `workspacePath`; branch = `branch`; status = `status`.
  NOTE: notifier warnings are printed to stderr/stdout BEFORE the json -> parse from first `[`/`{`.

DAEMON MODEL (nightly, headless): `ao start <repo>` runs a headless orchestrator + localhost:3000
dashboard as a LONG-RUNNING process (no desktop app, no GUI, no AppImage 404 - that was the
0.10.0 bug). This adapter does NOT start it (that is the supervisor's / operator's job); it only
probes `ao status` and fails with a clear message if the orchestrator is down.

RESULT CAPTURE IS RUNNER-AGNOSTIC. We do not rely on an AO "get diff" command. We read the
session's git worktree path (`workspacePath`) from `ao session ls --json` and compute the diff
with git ourselves (read-only; NO auto-merge, NO push).

If `ao` is not on PATH (e.g. AO not yet installed under WSL2), the adapter runs in SIMULATED
mode: it creates a real throwaway git worktree per task and makes a marker commit, so the rest
of the pipeline (worktree diff capture, vault summary) is exercised end-to-end against real git.
Simulated results are clearly flagged (`mode="simulated"`) and MUST NOT be treated as real
agent output.
"""
from __future__ import annotations
import json, os, re, shutil, subprocess, sys, time, pathlib

AO_BIN = os.environ.get("AO_BIN", "ao")

# --- AO command surface (edit here only) -----------------------------------------------------
# Templates are lists (argv). {sid}/{proj}/{agent}/{repo}/{prompt}/{msg} are substituted.
# Verified against @aoagents/ao@0.10.1-nightly (see module docstring).
AO_CMDS = {
    "daemon":     [AO_BIN, "status"],                                 # orchestrator health probe
    "project_add":[AO_BIN, "project", "add", "{repo}"],               # idempotent registration
    "project_ls": [AO_BIN, "project", "ls"],                          # TEXT: "<id> (<name>)"
    "set_default":[AO_BIN, "project", "set-default", "{proj}"],       # spawn targets default proj
    "spawn":      [AO_BIN, "spawn", "--agent", "{agent}", "--prompt", "{prompt}"],  # no id/branch
    "list":       [AO_BIN, "session", "ls", "--json", "-p", "{proj}"],# ONLY status source
    "kill":       [AO_BIN, "session", "kill", "{sid}"],
    "send":       [AO_BIN, "send", "{sid}", "{msg}"],
}
# Our task-level agent tag -> AO agent-plugin name (verified via `ao spawn --help`: codex, claude-code).
AGENT_MAP = {"claude": "claude-code", "codex": "codex"}

# Map AO nightly status strings -> our canonical set. Sources: `report` workflow states + session
# lifecycle states seen via `session ls --include-terminated`. Extend if new values appear.
STATUS_MAP = {
    # queued / spinning up
    "queued": "queued", "pending": "queued", "spawning": "queued", "started": "running",
    # actively working
    "running": "running", "working": "running", "in_progress": "running",
    "fixing_ci": "running", "addressing_reviews": "running",
    # needs the human
    "waiting": "blocked", "needs_input": "blocked", "blocked": "blocked", "stuck": "blocked",
    # done (incl. PR-created / review-ready, which we treat as "ready for human review")
    "pr_created": "completed", "draft_pr_created": "completed", "ready_for_review": "completed",
    "completed": "completed", "done": "completed", "merged": "completed", "verified": "completed",
    # failed / dead
    "failed": "failed", "error": "failed", "errored": "failed", "terminated": "failed",
    "killed": "failed", "crashed": "failed",
}
CANON = {"queued", "running", "blocked", "completed", "failed"}
# ---------------------------------------------------------------------------------------------


def have_ao() -> bool:
    return shutil.which(AO_BIN) is not None


def mode() -> str:
    return "live" if have_ao() else "simulated"


def _run(argv: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, encoding="utf-8")
    if check and r.returncode != 0:
        raise RuntimeError(f"cmd failed ({r.returncode}): {' '.join(argv)}\n{r.stderr.strip()}")
    return r


def _fmt(template: list[str], **kw) -> list[str]:
    return [part.format(**kw) for part in template]


def _git(worktree: str, *args: str, check: bool = True) -> str:
    return _run(["git", "-C", worktree, *args], check=check).stdout


# --- public adapter API ----------------------------------------------------------------------

def dispatch(task_id: str, prompt: str, agent: str, repo: str, project: str) -> dict:
    """Create one AO session for one small task. Returns a session record dict."""
    agent = AGENT_MAP.get(agent, agent)
    if have_ao():
        return _dispatch_live(task_id, prompt, agent, repo, project)
    return _dispatch_sim(task_id, prompt, agent, repo, project)


def poll(session: dict) -> str:
    """Return canonical status for a session record."""
    if session.get("mode") == "simulated":
        return "completed"  # sim work is done at dispatch time
    rec = _session_record(session["session_id"], session.get("project", ""))
    val = str((rec or {}).get("status") or "running").lower()
    return STATUS_MAP.get(val, "running")


def capture(session: dict, base_ref: str | None = None) -> dict:
    """Read-only capture of the session's git worktree diff + a short report. No merge/push."""
    wt = session["worktree"]
    base = base_ref or session.get("base_ref") or _default_base(wt)
    diff = _git(wt, "diff", f"{base}...HEAD", check=False)
    stat = _git(wt, "diff", "--stat", f"{base}...HEAD", check=False).strip()
    log = _git(wt, "log", "--oneline", f"{base}..HEAD", check=False).strip()
    report = (f"worktree: {wt}\nbase: {base}\nmode: {session.get('mode')}\n"
              f"commits:\n{log or '(none)'}\n\ndiffstat:\n{stat or '(no changes)'}")
    return {"diff": diff, "diffstat": stat, "report": report, "base": base}


def kill(session: dict) -> None:
    if session.get("mode") == "simulated":
        wt = session["worktree"]
        _run(["git", "-C", session["repo"], "worktree", "remove", "--force", wt], check=False)
        return
    _run(_fmt(AO_CMDS["kill"], sid=session["session_id"]), check=False)


# --- live impl -------------------------------------------------------------------------------

def _ensure_daemon() -> None:
    r = _run(AO_CMDS["daemon"], check=False)
    text = (r.stdout + r.stderr).lower()
    if "not running" in text or "no orchestrator" in text or "is not running" in text:
        raise RuntimeError(
            "AO orchestrator is not running. In @aoagents/ao 0.10.1-nightly it is HEADLESS: start "
            "it with `ao start <repo>` (long-running; no GUI) and keep it up, then retry. "
            "See scripts/RUNBOOK-ao-wsl2.md.")


def _resolve_project_id(repo: str) -> str:
    """Register the repo (idempotent) and read back AO's auto-assigned project id.

    Nightly assigns ids like `<name>_<hash>`; `project add` takes no --id and `project ls` is
    text only ("    <id> (<name>)"). We match on the repo's basename.
    """
    name = pathlib.Path(repo).name
    _run(_fmt(AO_CMDS["project_add"], repo=repo), check=False)  # ignore "already registered"
    out = _run(AO_CMDS["project_ls"], check=False).stdout
    # lines look like:  "    sample-repo_3b1d8c39bc (sample-repo)"
    matches = re.findall(r"^\s*(\S+)\s+\((.+?)\)\s*$", out, re.MULTILINE)
    ids = [pid for pid, nm in matches if nm == name]
    if not ids:
        raise RuntimeError(f"could not find AO project id for repo '{name}' in `ao project ls`:\n"
                           f"{out.strip()[:400]}")
    if len(ids) > 1:
        raise RuntimeError(f"ambiguous AO project id: {len(ids)} projects named '{name}' ({ids}). "
                           f"Register repos with distinct basenames.")
    return ids[0]


def _dispatch_live(task_id: str, prompt: str, agent: str, repo: str, project: str) -> dict:
    _ensure_daemon()
    pid = _resolve_project_id(repo)
    # nightly `ao spawn` has no --project: it targets the DEFAULT project. Point it at this repo.
    _run(_fmt(AO_CMDS["set_default"], proj=pid), check=False)
    # snapshot existing session ids so we can identify the one this spawn creates (spawn prints
    # no id and auto-names the session, e.g. "sr-1").
    before = {s["id"] for s in _list_sessions(pid) if s.get("id")}
    r = _run(_fmt(AO_CMDS["spawn"], agent=agent, prompt=prompt), cwd=repo, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"ao spawn failed for {task_id}: {(r.stderr or r.stdout).strip()}")
    sid, wt, branch = _find_new_session(pid, before)
    return {"mode": "live", "session_id": sid, "worktree": wt, "repo": repo,
            "base_ref": None, "agent": agent, "project": pid, "branch": branch}


def _find_new_session(pid: str, before: set[str]) -> tuple[str, str, str | None]:
    """Identify the session created since `before` (retry briefly - registration is async)."""
    match = None
    for _ in range(10):
        new = [s for s in _list_sessions(pid) if s.get("id") and s["id"] not in before]
        if new:
            match = sorted(new, key=lambda s: s.get("lastActivityAt") or "")[-1]
            break
        time.sleep(1)
    if match is None:
        raise RuntimeError(f"spawned session not found for project {pid} "
                           f"(no new id beyond {sorted(before)}). Check `ao session ls --json`.")
    sid = str(match.get("id") or "")
    wt = _session_worktree(match)
    if not (sid and wt):
        raise RuntimeError(f"could not resolve session id/worktree (keys={list(match)}). "
                           f"Fix _find_new_session/_session_worktree in ao_runner.")
    return sid, wt, match.get("branch")


def _session_record(sid: str, pid: str) -> dict | None:
    for s in _list_sessions(pid):
        if str(s.get("id")) == str(sid):
            return s
    return None


def _list_sessions(pid: str) -> list:
    r = _run(_fmt(AO_CMDS["list"], proj=pid), check=False)
    return _load_sessions(r.stdout)


def _session_worktree(s: dict) -> str | None:
    # confirmed key is `workspacePath`; keep older candidates as defensive fallbacks.
    return (s.get("workspacePath") or s.get("worktree") or s.get("worktreePath")
            or s.get("path") or s.get("workdir") or s.get("workspace"))


def _load_sessions(text: str) -> list:
    obj = _first_json(text, want=("[", "{"))
    if isinstance(obj, dict):
        obj = obj.get("data") or obj.get("sessions") or obj.get("items") or []
    return obj if isinstance(obj, list) else []


# --- simulated impl (real git worktree, stubbed "agent") -------------------------------------

def _dispatch_sim(task_id: str, prompt: str, agent: str, repo: str, project: str) -> dict:
    if not (pathlib.Path(repo) / ".git").exists():
        raise RuntimeError(f"simulated mode needs a git repo at {repo} (run the sample setup).")
    base = _default_base(repo)
    branch = f"ao-sim/{task_id}"
    wt = str(pathlib.Path(repo).parent / f".ao-sim-worktrees/{task_id}")
    pathlib.Path(wt).parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "-C", repo, "worktree", "add", "-B", branch, wt, base], check=True)
    # Stubbed agent "work": leave a marker file + commit so the diff is REAL.
    marker = pathlib.Path(wt) / f"AO_SIM_{task_id}.md"
    marker.write_text(
        f"# Simulated agent output for {task_id}\n\n"
        f"Agent: {agent}\n\nTask prompt:\n\n> {prompt.strip()}\n\n"
        f"_This file was produced by ao_runner SIMULATED mode - not a real coding agent._\n",
        encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "-c", "user.email=ao-sim@local", "-c", "user.name=ao-sim",
         "commit", "-m", f"[sim] {task_id}: {prompt.strip()[:60]}")
    return {"mode": "simulated", "session_id": f"sim-{task_id}", "worktree": wt,
            "repo": repo, "base_ref": base, "agent": agent, "project": project}


# --- helpers ---------------------------------------------------------------------------------

def _default_base(worktree_or_repo: str) -> str:
    for ref in ("origin/HEAD", "main", "master"):
        r = _run(["git", "-C", worktree_or_repo, "rev-parse", "--verify", "-q", ref], check=False)
        if r.returncode == 0:
            return ref.split("/")[-1] if ref == "origin/HEAD" else ref
    # fall back to the repo's first commit
    r = _run(["git", "-C", worktree_or_repo, "rev-list", "--max-parents=0", "HEAD"], check=False)
    return r.stdout.split()[0] if r.stdout.strip() else "HEAD"


def _first_json(text: str, want: tuple[str, ...] = ("{",)):
    """Extract the first balanced JSON value from output that AO prefixes with notifier warnings.

    `want` lists acceptable opening chars ("{" object, "[" array). Returns the parsed value, or
    None if none is found (callers that need a value should check/raise). Nightly `session ls`
    prints several "[notifier-*] No ... configured" lines before the json array.
    """
    text = (text or "").strip()
    if not text:
        return None
    close = {"{": "}", "[": "]"}
    # AO prefixes real json with "[notifier-*] ..." lines that also contain brackets, so we can't
    # just take the first bracket. Try every candidate opening position, return the first that
    # yields a balanced, parseable JSON value.
    for start in range(len(text)):
        opn = text[start]
        if opn not in want:
            continue
        cls, depth = close[opn], 0
        for i in range(start, len(text)):
            depth += (text[i] == opn) - (text[i] == cls)
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    break  # not real json at this start; advance to the next candidate
    return None


if __name__ == "__main__":  # tiny self-check
    print(f"AO adapter mode: {mode()}  (AO_BIN={AO_BIN!r}, on PATH={have_ao()})")
