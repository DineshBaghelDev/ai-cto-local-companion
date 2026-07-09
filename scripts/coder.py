#!/usr/bin/env python3
"""coder.py - CTO/manager <-> coding-runner glue for the AI-CTO system.

Flow (matches workflows/agentic-coding.md):
  1. read an APPROVED PRD from the Markdown vault
  2. split its "## Implementation Tasks" checklist into small tasks
  3. dispatch each task to the decided runner (AO) via its headless surface (ao_runner.py)
  4. runner runs Codex / Claude Code in a separate git worktree per task
  5. capture results as diffs + reports (read-only; NO auto-merge)
  6. write an output summary back into the vault for the human to review

Runtime state (task metadata, session ids, statuses) lives in a SQLite DB OUTSIDE the vault
(~/.ai-cto/coder.db). The vault only receives the human-readable run summary.

The runner is NOT chosen here - it is whatever decisions/coding-runner.md names. All runner
specifics are isolated in ao_runner.py. This file is runner-agnostic glue.

PRD contract (produced by the PRD-generation skill):
  - a Markdown note in the vault with frontmatter `type: prd` and `status: approved`
  - an `## Implementation Tasks` section of checklist bullets, e.g.
        - [ ] Add a --version flag to the CLI (agent: codex)
        - [ ] Write a smoke test for the greet command (agent: claude) (dir: tests)
    `(agent: codex|claude)` and `(dir: <relpath>)` are optional per-line hints.

Usage:
  python coder.py dispatch --prd <vault/prds/foo.md> --repo <path> [--default-agent claude]
                            [--project ai-cto] [--force]
  python coder.py poll     [--run <run_id>]
  python coder.py collect  [--run <run_id>]      # writes patches + vault summary
  python coder.py status   [--run <run_id>]
  python coder.py preview  --run <run_id> --cmd "<command to run in each worktree>"
  python coder.py feedback --task <task_id> --message "..."
  python coder.py pr       --task <task_id> [--title "..."] [--body "..."]
  python coder.py merge-pr --pr <number-or-url> [--method squash]
"""
from __future__ import annotations
import argparse, datetime, os, pathlib, re, shlex, shutil, sqlite3, subprocess, sys, uuid

import ao_runner  # the isolated runner adapter

VAULT = pathlib.Path(os.environ.get("AI_CTO_VAULT", r"E:\Projects\AI CTO\vault"))
STATE_DIR = pathlib.Path(os.environ.get("AI_CTO_STATE", pathlib.Path.home() / ".ai-cto"))
DB_PATH = STATE_DIR / "coder.db"
PATCH_DIR = STATE_DIR / "patches"
PROJECT = os.environ.get("BASIC_MEMORY_PROJECT", "ai-cto")
NOW = lambda: datetime.datetime.now().isoformat(timespec="seconds")


# --- db ---------------------------------------------------------------------------------------

def db() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript("""
      CREATE TABLE IF NOT EXISTS runs(
        run_id TEXT PRIMARY KEY, prd_path TEXT, prd_title TEXT, repo TEXT,
        mode TEXT, created_at TEXT);
      CREATE TABLE IF NOT EXISTS tasks(
        task_id TEXT PRIMARY KEY, run_id TEXT, seq INTEGER, text TEXT, agent TEXT,
        session_id TEXT, worktree TEXT, project TEXT, status TEXT, diff_path TEXT,
        created_at TEXT, updated_at TEXT);
      CREATE TABLE IF NOT EXISTS activity(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, run_id TEXT,
        task_id TEXT, message TEXT);
    """)
    return con


def log_activity(kind: str, message: str, run_id: str | None = None,
                 task_id: str | None = None) -> None:
    con = db()
    _log_activity_con(con, kind, message, run_id, task_id)
    con.commit()


def _log_activity_con(con: sqlite3.Connection, kind: str, message: str,
                      run_id: str | None = None, task_id: str | None = None) -> None:
    stamp = NOW()
    con.execute("INSERT INTO activity(ts, kind, run_id, task_id, message) VALUES (?,?,?,?,?)",
                (stamp, kind, run_id, task_id, message))
    if run_id:
        _append_task_log(run_id, f"- **{stamp}** `{kind}`"
                         f"{' ' + task_id if task_id else ''}: {message}\n")


def recent_activity(limit: int = 10) -> list[dict]:
    con = db()
    rows = con.execute("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def status_snapshot(limit: int = 8) -> dict:
    con = db()
    latest = con.execute("SELECT run_id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
    tasks = [dict(r) for r in _run_tasks(con, latest["run_id"] if latest else None)] if latest else []
    active = [t for t in tasks if t["status"] not in ("completed", "failed")]
    return {"latest_run": latest["run_id"] if latest else None,
            "tasks": tasks[:limit], "active": active[:limit],
            "activity": recent_activity(limit)}


# --- PRD parsing --------------------------------------------------------------------------------

FM = re.compile(r"^---\s*$(.*?)^---\s*$", re.S | re.M)
TASK_HEADING = re.compile(r"^#{1,6}\s+(implementation tasks|tasks)\s*$", re.I | re.M)
BULLET = re.compile(r"^\s*[-*]\s+\[[ xX]\]\s+(.*)$")
AGENT_HINT = re.compile(r"\(agent:\s*(claude|codex)\s*\)", re.I)
DIR_HINT = re.compile(r"\(dir:\s*([^)]+)\)", re.I)


def parse_prd(path: pathlib.Path) -> tuple[dict, list[dict]]:
    text = path.read_text(encoding="utf-8")
    fm = {}
    m = FM.search(text)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip().lower()] = v.strip()
    # tasks = checklist bullets under the Implementation Tasks heading, up to the next heading
    tasks: list[dict] = []
    h = TASK_HEADING.search(text)
    if h:
        rest = text[h.end():]
        nxt = re.search(r"^#{1,6}\s+", rest, re.M)
        block = rest[: nxt.start()] if nxt else rest
        for line in block.splitlines():
            b = BULLET.match(line)
            if not b:
                continue
            raw = b.group(1).strip()
            agent = (AGENT_HINT.search(raw).group(1).lower() if AGENT_HINT.search(raw) else None)
            wdir = (DIR_HINT.search(raw).group(1).strip() if DIR_HINT.search(raw) else None)
            clean = AGENT_HINT.sub("", DIR_HINT.sub("", raw)).strip(" .")
            tasks.append({"text": clean, "agent": agent, "dir": wdir})
    return fm, tasks


# --- commands -----------------------------------------------------------------------------------

def cmd_dispatch(a) -> None:
    prd = pathlib.Path(a.prd)
    if not prd.is_absolute():
        prd = (VAULT / a.prd) if (VAULT / a.prd).exists() else prd
    if not prd.exists():
        sys.exit(f"PRD not found: {prd}")
    repo = pathlib.Path(a.repo).resolve()
    if not (repo / ".git").exists():
        sys.exit(f"--repo is not a git repo: {repo}")

    fm, tasks = parse_prd(prd)
    if not a.force and fm.get("status", "").lower() != "approved":
        sys.exit(f"PRD status is {fm.get('status')!r}, not 'approved'. Use --force to override.")
    if not tasks:
        sys.exit("No '## Implementation Tasks' checklist items found in the PRD.")

    run_id = "run-" + datetime.date.today().isoformat() + "-" + uuid.uuid4().hex[:6]
    con = db()
    con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)",
                (run_id, str(prd), fm.get("title", prd.stem), str(repo),
                 ao_runner.mode(), NOW()))
    _log_activity_con(con, "dispatched",
                      f"Dispatching {len(tasks)} task(s) for {fm.get('title', prd.stem)}", run_id)
    print(f"Runner mode: {ao_runner.mode()}   run: {run_id}   PRD: {fm.get('title', prd.stem)}")
    for i, t in enumerate(tasks, 1):
        task_id = f"{run_id}-t{i:02d}"
        agent = t["agent"] or a.default_agent
        prompt = _task_prompt(fm, t)
        try:
            sess = ao_runner.dispatch(task_id, prompt, agent, str(repo), a.project)
            status = ao_runner.poll(sess)
            con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (task_id, run_id, i, t["text"], agent, sess["session_id"],
                         sess["worktree"], sess.get("project"), status, None, NOW(), NOW()))
            _log_activity_con(con, status, t["text"], run_id, task_id)
            print(f"  [{i:02d}] {agent:<6} -> {sess['session_id']:<24} {status}  | {t['text'][:52]}")
        except Exception as e:  # noqa: BLE001 - record the failure, keep going
            con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (task_id, run_id, i, t["text"], agent, None, None, None,
                         "failed", None, NOW(), NOW()))
            _log_activity_con(con, "failed", f"{t['text']} ({e})", run_id, task_id)
            print(f"  [{i:02d}] {agent:<6} -> DISPATCH FAILED: {e}", file=sys.stderr)
    con.commit()
    print(f"\nNext: python coder.py poll --run {run_id}   then   collect --run {run_id}")


def cmd_poll(a) -> None:
    con = db()
    rows = _run_tasks(con, a.run)
    for r in rows:
        if r["status"] in ("completed", "failed") or not r["session_id"]:
            continue
        sess = _session_of(r)
        try:
            st = ao_runner.poll(sess)
        except Exception as e:  # noqa: BLE001
            st = "failed"; print(f"  poll {r['task_id']}: {e}", file=sys.stderr)
        con.execute("UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                    (st, NOW(), r["task_id"]))
        log_activity(st, r["text"], r["run_id"], r["task_id"])
        print(f"  {r['task_id']}: {st}")
    con.commit()
    _print_status(con, a.run)


def cmd_collect(a) -> None:
    con = db()
    PATCH_DIR.mkdir(parents=True, exist_ok=True)
    rows = _run_tasks(con, a.run)
    if not rows:
        sys.exit("no tasks for that run")
    run_id = rows[0]["run_id"]
    collected = []
    for r in rows:
        if not r["session_id"]:
            collected.append((r, None)); continue
        sess = _session_of(r)
        # refresh status first
        try:
            st = ao_runner.poll(sess)
            con.execute("UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                        (st, NOW(), r["task_id"]))
        except Exception:  # noqa: BLE001
            st = r["status"]
        cap = None
        if st == "completed":
            try:
                cap = ao_runner.capture(sess)
                patch = PATCH_DIR / f"{r['task_id']}.patch"
                patch.write_text(cap["diff"] or "", encoding="utf-8")
                con.execute("UPDATE tasks SET diff_path=?, updated_at=? WHERE task_id=?",
                            (str(patch), NOW(), r["task_id"]))
            except Exception as e:  # noqa: BLE001
                print(f"  capture {r['task_id']}: {e}", file=sys.stderr)
        collected.append((dict(r) | {"status": st}, cap))
    con.commit()
    note = _write_vault_summary(con, run_id, collected)
    log_activity("collected", f"Vault summary written: {note}", run_id)
    print(f"\nPatches: {PATCH_DIR}")
    print(f"Vault summary written: {note}")
    print("NO AUTO-MERGE. Review each patch, then merge manually if approved.")


def cmd_status(a) -> None:
    _print_status(db(), a.run)


def cmd_preview(a) -> None:
    con = db()
    rows = [r for r in _run_tasks(con, a.run) if r["worktree"]]
    if not rows:
        sys.exit("no worktrees for that run")
    run_id = rows[0]["run_id"]
    out = VAULT / "coding-runs" / f"{run_id}-preview.md"
    lines = [
        "---", f"title: Preview {run_id}", "type: coding-preview",
        f"permalink: coding-runs/{run_id}-preview", "tags: [coding-run, preview]",
        "---", "", f"# Preview {run_id}", "",
        f"- **Command:** `{a.cmd}`", f"- **Ran:** {NOW()}", "",
    ]
    for r in rows:
        wt = pathlib.Path(r["worktree"])
        lines += [f"## {r['task_id']}", "", f"- **Task:** {r['text']}", f"- **Worktree:** `{wt}`", ""]
        if not wt.exists():
            lines += ["Worktree missing.", ""]
            continue
        p = subprocess.run(a.cmd, cwd=wt, shell=True, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=a.timeout)
        lines += [f"- **Exit code:** {p.returncode}", "", "### stdout", "```",
                  (p.stdout or "")[-6000:] or "(empty)", "```", "",
                  "### stderr", "```", (p.stderr or "")[-6000:] or "(empty)", "```", ""]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Preview written: {out}")


def cmd_feedback(a) -> None:
    con = db()
    r = _task_by_id(con, a.task)
    msg = f"HUMAN FEEDBACK: {a.message}. Continue in this worktree. Do not merge or push."
    if r["session_id"] and not str(r["session_id"]).startswith("sim-"):
        ao_runner._run(ao_runner._fmt(ao_runner.AO_CMDS["send"], sid=r["session_id"], msg=msg))
        status = "running"
    else:
        wt = pathlib.Path(r["worktree"] or "")
        if not wt.exists():
            sys.exit(f"worktree missing for {a.task}: {wt}")
        marker = wt / f"AO_FEEDBACK_{a.task}.md"
        marker.write_text(f"# Human feedback\n\n{msg}\n", encoding="utf-8")
        _git_cmd(wt, "add", "-A")
        _git_cmd(wt, "-c", "user.email=ao-sim@local", "-c", "user.name=ao-sim",
                 "commit", "-m", f"[sim] feedback {a.task}")
        status = "completed"
    _append_task_log(r["run_id"], f"- **{NOW()}** `{a.task}` feedback: {a.message}\n")
    con.execute("UPDATE tasks SET status=?, updated_at=? WHERE task_id=?", (status, NOW(), a.task))
    con.commit()
    log_activity("feedback", a.message, r["run_id"], a.task)
    print(f"{a.task}: feedback sent -> {status}")


def start_task(repo: str | None, title: str, task: str, agent: str = "claude") -> dict:
    repo_path = pathlib.Path(repo or os.environ.get("AI_CTO_DEFAULT_REPO", "")).resolve()
    log_activity("request", f"{title}: {task}")
    if not repo_path or not (repo_path / ".git").exists():
        msg = "No default git repo is configured. Start with .\\scripts\\start.ps1 -Repo <path> or pass --repo."
        log_activity("blocked", msg)
        return {"ok": False, "error": msg}

    folder = VAULT / "prds"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{_slug(title)}.md"
    today = datetime.date.today().isoformat()
    path.write_text("\n".join([
        "---", f"title: {title}", "type: prd", "status: approved",
        f"permalink: prds/{_slug(title)}", "tags: [prd, jarvis-task]", "---", "",
        f"# PRD: {title}", "", f"- **Date:** {today}",
        "- **Source:** Jarvis V1 task request", "", "## Goal", "", task, "",
        "## Implementation Tasks", "", f"- [ ] {task} (agent: {agent})", "",
        "## Relations", "- part_of [[Active Projects]]", "",
    ]), encoding="utf-8")
    log_activity("prd-created", f"Created and approved {path}")
    class A:
        prd = str(path)
        repo = str(repo_path)
        default_agent = agent
        project = PROJECT
        force = False
    cmd_dispatch(A)
    run_id = db().execute("SELECT run_id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()["run_id"]
    _append_task_log(run_id, f"- **{NOW()}** `request`: {title}: {task}\n")
    _append_task_log(run_id, f"- **{NOW()}** `prd-created`: {path}\n")
    return {"ok": True, "run_id": run_id, "prd": str(path), "repo": str(repo_path)}


def cmd_pr(a) -> None:
    if not _have_gh():
        sys.exit("GitHub CLI `gh` not found on Windows or WSL. Install/login to gh, then retry.")
    con = db()
    r = _task_by_id(con, a.task)
    wt = pathlib.Path(r["worktree"] or "")
    if not wt.exists():
        sys.exit(f"worktree missing for {a.task}: {wt}")
    branch = _git_cmd(wt, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if not branch or branch == "HEAD":
        sys.exit(f"cannot determine branch for {wt}")
    _git_cmd(wt, "push", "-u", "origin", branch)
    title = a.title or f"{a.task}: {r['text'][:70]}"
    body = a.body or f"Task: {r['text']}\n\nRun: {r['run_id']}\n\nCreated by local AI-CTO glue."
    pr = _gh(["pr", "create", "--title", title, "--body", body], cwd=wt)
    _append_task_log(r["run_id"], f"- **{NOW()}** `{a.task}` PR created: {pr.strip()}\n")
    print(pr.strip())


def cmd_merge_pr(a) -> None:
    if not _have_gh():
        sys.exit("GitHub CLI `gh` not found on Windows or WSL. Install/login to gh, then retry.")
    repo = pathlib.Path(a.repo).resolve() if a.repo else pathlib.Path.cwd()
    cmd = ["gh", "pr", "merge", str(a.pr), f"--{a.method}", "--delete-branch"]
    print((_gh(cmd[1:], cwd=repo) or "merged").strip())


# --- helpers ------------------------------------------------------------------------------------

def _task_prompt(fm: dict, t: dict) -> str:
    ctx = f"PRD: {fm.get('title', 'untitled')}\n" if fm.get("title") else ""
    dir_line = f"\nWork within: {t['dir']}" if t.get("dir") else ""
    return (f"{ctx}Implement this single, small task and commit your work:\n\n"
            f"  {t['text']}{dir_line}\n\n"
            f"Keep the change minimal and focused. Do not merge or push.")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "untitled"


def _run_tasks(con, run: str | None) -> list[sqlite3.Row]:
    if run:
        return con.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY seq", (run,)).fetchall()
    last = con.execute("SELECT run_id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
    if not last:
        return []
    return con.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY seq",
                       (last["run_id"],)).fetchall()


def _session_of(r: sqlite3.Row) -> dict:
    sid = r["session_id"] or ""
    return {"mode": "simulated" if sid.startswith("sim-") else "live",
            "session_id": sid, "worktree": r["worktree"],
            "repo": r["worktree"], "base_ref": None, "project": r["project"]}


def _task_by_id(con, task_id: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        sys.exit(f"unknown task: {task_id}")
    return row


def _git_cmd(wt: pathlib.Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(wt), *args], text=True, capture_output=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        sys.exit(p.stderr.strip())
    return p.stdout


def _append_task_log(run_id: str, text: str) -> None:
    log = VAULT / "coding-runs" / f"{run_id}-log.md"
    if not log.exists():
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("\n".join([
            "---", f"title: Task Log {run_id}", "type: task-log",
            f"permalink: coding-runs/{run_id}-log", "tags: [task-log]", "---", "",
            f"# Task Log - {run_id}", "",
        ]), encoding="utf-8")
    with open(log, "a", encoding="utf-8") as f:
        f.write(text)


def _have_gh() -> bool:
    if shutil.which("gh"):
        return True
    p = subprocess.run(["wsl", "-e", "bash", "-lc", "command -v gh"],
                       text=True, capture_output=True, encoding="utf-8")
    return p.returncode == 0


def _gh(args: list[str], cwd: pathlib.Path) -> str:
    if shutil.which("gh"):
        p = subprocess.run(["gh", *args], cwd=cwd, text=True, capture_output=True, encoding="utf-8")
    else:
        wp = subprocess.run(["wsl", "wslpath", "-a", str(cwd)], text=True,
                            capture_output=True, encoding="utf-8")
        if wp.returncode != 0:
            sys.exit(wp.stderr.strip())
        script = "cd " + shlex.quote(wp.stdout.strip()) + " && gh " + " ".join(shlex.quote(a) for a in args)
        p = subprocess.run(["wsl", "-e", "bash", "-lc", script], text=True,
                           capture_output=True, encoding="utf-8")
    if p.returncode != 0:
        sys.exit(p.stderr.strip() or p.stdout.strip())
    return p.stdout


def _print_status(con, run: str | None) -> None:
    rows = _run_tasks(con, run)
    if not rows:
        print("(no tasks)"); return
    print(f"\nrun {rows[0]['run_id']}  ({ _mode_of(con, rows[0]['run_id']) })")
    for r in rows:
        diff = "patch" if r["diff_path"] else "-"
        print(f"  [{r['seq']:02d}] {r['status']:<10} {r['agent']:<6} {diff:<6} {r['text'][:56]}")


def _mode_of(con, run_id: str) -> str:
    row = con.execute("SELECT mode FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return row["mode"] if row else "?"


def _write_vault_summary(con, run_id: str, collected: list[tuple]) -> pathlib.Path:
    run = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    folder = VAULT / "coding-runs"
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{run_id}.md"
    done = sum(1 for r, _ in collected if r["status"] == "completed")
    lines = [
        "---", f"title: Coding Run {run_id}", "type: coding-run",
        f"permalink: coding-runs/{run_id}", "tags: [coding-run, review-needed]",
        f"status: {'awaiting-review' if done else 'incomplete'}", "---", "",
        f"# Coding Run {run_id}", "",
        f"- **PRD:** {run['prd_title']}  (`{run['prd_path']}`)",
        f"- **Repo:** `{run['repo']}`",
        f"- **Runner:** AgentWrapper/agent-orchestrator (AO) - see [[Decision - Coding Runner]]",
        f"- **Runner mode:** `{run['mode']}`"
        + ("  WARNING: SIMULATED - not real agent output" if run["mode"] == "simulated" else ""),
        f"- **Dispatched:** {run['created_at']}   **Collected:** {NOW()}",
        f"- **Completed:** {done}/{len(collected)} tasks", "",
        "> WARNING: **No auto-merge.** Each task ran in its own git worktree. Review the patch below,",
        "> then merge manually if approved.", "",
        "## Tasks", "",
    ]
    for r, cap in collected:
        lines.append(f"### [{r['seq']:02d}] {r['text']}")
        lines.append(f"- agent: `{r['agent']}`  |  status: **{r['status']}**  "
                     f"|  session: `{r['session_id'] or '-'}`")
        if r.get("diff_path"):
            lines.append(f"- patch: `{r['diff_path']}`")
        if cap and cap.get("diffstat"):
            lines.append("- diffstat:")
            lines.append("```")
            lines.append(cap["diffstat"])
            lines.append("```")
        lines.append("")
    lines += ["## Review checklist", "",
              "- [ ] Reviewed each patch for correctness", "- [ ] Ran tests locally",
              "- [ ] Merged approved changes manually", "",
              "## Relations", "- part_of [[Index]]",
              "- implements [[Agentic Coding Workflow]]", ""]
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="CTO <-> coding-runner glue (runner: AO).")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dispatch"); d.add_argument("--prd", required=True)
    d.add_argument("--repo", required=True); d.add_argument("--project", default=PROJECT)
    d.add_argument("--default-agent", default="claude", choices=["claude", "codex"])
    d.add_argument("--force", action="store_true"); d.set_defaults(fn=cmd_dispatch)
    for name, fn in (("poll", cmd_poll), ("collect", cmd_collect), ("status", cmd_status)):
        s = sub.add_parser(name); s.add_argument("--run", default=None); s.set_defaults(fn=fn)
    pv = sub.add_parser("preview"); pv.add_argument("--run", required=True)
    pv.add_argument("--cmd", required=True); pv.add_argument("--timeout", type=int, default=60)
    pv.set_defaults(fn=cmd_preview)
    fb = sub.add_parser("feedback"); fb.add_argument("--task", required=True)
    fb.add_argument("--message", required=True); fb.set_defaults(fn=cmd_feedback)
    pr = sub.add_parser("pr"); pr.add_argument("--task", required=True)
    pr.add_argument("--title"); pr.add_argument("--body"); pr.set_defaults(fn=cmd_pr)
    mg = sub.add_parser("merge-pr"); mg.add_argument("--pr", required=True)
    mg.add_argument("--repo"); mg.add_argument("--method", choices=["squash", "merge", "rebase"], default="squash")
    mg.set_defaults(fn=cmd_merge_pr)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
