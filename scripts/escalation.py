#!/usr/bin/env python3
"""escalation.py - blocker escalation glue for the AI-CTO system.

When a coding-agent task (dispatched by coder.py to the runner named in
decisions/coding-runner.md, i.e. AO) becomes BLOCKED, this module:

  1. detects it (task status from ao_runner.poll / coder.db)
  2. writes a structured blocker report to the vault  -> vault/blockers/<task_id>.md
     (task, worktree/branch, what failed, options, decision needed, recommendation;
      drafted by Claude via the Claude Code subscription when available, template otherwise)
  3. fires a LOCAL desktop toast (Windows, zero deps, zero cloud)  <- first-line alert
  4. surfaces it on the voice dashboard (bot.py /api/blockers)
  5. the voice brain (voice/brain.py) explains it and takes the human's spoken decision
     via the `resolve_blocker` tool, which calls resolve() + resume() here
  6. the decision is saved to: the blocker note, the run task log
     (vault/coding-runs/<run>-log.md), the project decisions page (vault/decisions.md),
     and the PRD note; task goes blocked -> resume-ready -> resumed
  7. resume: live AO session gets `ao send <sid> <decision>`; simulated sessions get a
     real marker commit in their worktree. NO auto-merge, NO deploy, NO phone/push.

Escalation transport is desktop toast + local pipecat voice session ONLY (Twilio/phone
paths are excluded by the zero-paid-APIs hard constraint - see CLAUDE.md).

Usage:
  python escalation.py list                       # blockers + states
  python escalation.py watch [--interval 15]      # poll: detect blocked, auto-resume ready
  python escalation.py detect                     # one detection pass
  python escalation.py simulate --task <task_id> [--context "..."]   # demo: inject blocker
  python escalation.py resolve --task <task_id> --decision "..." [--rationale "..."]
  python escalation.py resume  --task <task_id>
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import ao_runner  # runner adapter - the only AO-specific surface

VAULT = pathlib.Path(os.environ.get("AI_CTO_VAULT", r"E:\Projects\AI CTO\vault"))
STATE_DIR = pathlib.Path(os.environ.get("AI_CTO_STATE", pathlib.Path.home() / ".ai-cto"))
DB_PATH = STATE_DIR / "coder.db"
NOW = lambda: datetime.datetime.now().isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript("""
      CREATE TABLE IF NOT EXISTS blockers(
        task_id TEXT PRIMARY KEY, run_id TEXT, status TEXT, context TEXT,
        report_path TEXT, decision TEXT, rationale TEXT,
        created_at TEXT, decided_at TEXT, resumed_at TEXT);
    """)
    return con


def _task(con: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        sys.exit(f"unknown task: {task_id}")
    return row


def _branch_of(worktree: str | None) -> str:
    if not worktree or not pathlib.Path(worktree).exists():
        return "(worktree gone)"
    r = subprocess.run(["git", "-C", worktree, "rev-parse", "--abbrev-ref", "HEAD"],
                       capture_output=True, text=True, encoding="utf-8")
    return r.stdout.strip() or "(unknown)"


# --- desktop notification (local, free, zero deps) ---------------------------------------------

def notify(title: str, body: str) -> bool:
    """Windows toast via WinRT from PowerShell. Local only - no cloud, no push service."""
    ps = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t = $xml.GetElementsByTagName("text")
$t.Item(0).AppendChild($xml.CreateTextNode({json.dumps(title)})) | Out-Null
$t.Item(1).AppendChild($xml.CreateTextNode({json.dumps(body)})) | Out-Null
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("AI-CTO").Show(
    [Windows.UI.Notifications.ToastNotification]::new($xml))
"""
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        print(f"toast failed (non-fatal): {r.stderr.strip()[:200]}", file=sys.stderr)
    return r.returncode == 0


# --- blocker report -----------------------------------------------------------------------------

REPORT_PROMPT = """You are the CTO companion. A coding agent hit a blocker. Write a SHORT blocker
report in Markdown with EXACTLY these five sections (## headings): "What failed",
"Options", "Decision needed", "Recommendation", "Voice summary".
- "Options" is a numbered list (2-3 realistic options).
- "Recommendation" names ONE option and why, in 1-2 sentences.
- "Voice summary" is 2-3 spoken-style sentences covering the failure, the options,
  and your recommendation (it will be read aloud).
No preamble, no code fences around the whole answer.

Task: {task_text}
Agent: {agent}
Branch: {branch}
Blocker context (logs/status):
{context}
"""


def _claude_exe() -> str | None:
    """Find a runnable claude CLI shim (.exe/.cmd; shutil.which misses .ps1-only hits)."""
    for name in ("claude.exe", "claude.cmd", "claude"):
        exe = shutil.which(name)
        if exe and not exe.lower().endswith(".ps1"):
            return exe
    fallback = pathlib.Path.home() / ".local/bin/claude.exe"
    return str(fallback) if fallback.exists() else None


def _draft_report_body(task: sqlite3.Row, branch: str, context: str) -> tuple[str, str]:
    """Return (body_markdown, author). Claude subscription first, template fallback."""
    claude = _claude_exe()
    if claude:
        prompt = REPORT_PROMPT.format(task_text=task["text"], agent=task["agent"],
                                      branch=branch, context=context or "(none captured)")
        try:
            r = subprocess.run(
                [claude, "-p", prompt, "--max-turns", "1"],
                capture_output=True, text=True, encoding="utf-8", timeout=120)
            body = (r.stdout or "").strip()
            if r.returncode == 0 and "## What failed" in body:
                return body, "claude (subscription)"
        except subprocess.TimeoutExpired:
            pass
    body = (f"## What failed\n\n{context or 'Agent reported blocked; no detail captured.'}\n\n"
            f"## Options\n\n1. Human inspects the worktree and unblocks manually.\n"
            f"2. Abort this task and re-scope it in the PRD.\n\n"
            f"## Decision needed\n\nWhich option should the agent follow?\n\n"
            f"## Recommendation\n\nOption 1 - inspect first; the failure context is thin.\n\n"
            f"## Voice summary\n\nA coding task is blocked: {context or 'no details captured'}. "
            f"You can unblock it manually or abort and re-scope. I suggest inspecting first.")
    return body, "template (claude CLI unavailable)"


def create_blocker(task_id: str, context: str) -> pathlib.Path:
    con = db()
    t = _task(con, task_id)
    existing = con.execute("SELECT * FROM blockers WHERE task_id=?", (task_id,)).fetchone()
    if existing and existing["status"] == "pending":
        print(f"blocker already pending for {task_id}")
        return pathlib.Path(existing["report_path"])

    branch = _branch_of(t["worktree"])
    body, author = _draft_report_body(t, branch, context)
    folder = VAULT / "blockers"
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{task_id}.md"
    out.write_text("\n".join([
        "---", f"title: Blocker - {task_id}", "type: blocker",
        f"permalink: blockers/{task_id}", "tags: [blocker, escalation, pending]",
        "status: pending", "---", "",
        f"# Blocker: {task_id}", "",
        f"- **Task:** {t['text']}",
        f"- **Run:** {t['run_id']}   **Agent:** `{t['agent']}`   **Session:** `{t['session_id']}`",
        f"- **Worktree:** `{t['worktree']}`",
        f"- **Branch:** `{branch}`",
        f"- **Raised:** {NOW()}   **Report drafted by:** {author}", "",
        body, "",
        "## Relations", "- part_of [[Index]]", "- relates_to [[Decision - Coding Runner]]", "",
    ]), encoding="utf-8")

    con.execute("INSERT OR REPLACE INTO blockers VALUES (?,?,?,?,?,?,?,?,?,?)",
                (task_id, t["run_id"], "pending", context, str(out), None, None, NOW(), None, None))
    con.execute("UPDATE tasks SET status='blocked', updated_at=? WHERE task_id=?", (NOW(), task_id))
    con.commit()
    notify("Coding agent blocked", f"{task_id}: {t['text'][:80]} - open the voice companion "
           f"(localhost:7860) to decide.")
    print(f"blocker raised: {out}")
    return out


# --- detection ----------------------------------------------------------------------------------

def detect() -> list[str]:
    """One pass: refresh live task statuses; raise blockers for newly-blocked tasks."""
    con = db()
    raised = []
    rows = con.execute("SELECT * FROM tasks WHERE status NOT IN "
                       "('completed','failed','resume-ready')").fetchall()
    for r in rows:
        if not r["session_id"]:
            continue
        sess = {"mode": "simulated" if r["session_id"].startswith("sim-") else "live",
                "session_id": r["session_id"], "worktree": r["worktree"],
                "repo": r["worktree"], "base_ref": None, "project": r["project"]}
        try:
            st = ao_runner.poll(sess)
        except Exception as e:  # noqa: BLE001
            print(f"poll {r['task_id']}: {e}", file=sys.stderr)
            continue
        # a simulated blocker injected into the DB must not be "un-blocked" by sim poll
        if r["status"] == "blocked" and sess["mode"] == "simulated":
            st = "blocked"
        if st != r["status"]:
            con.execute("UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                        (st, NOW(), r["task_id"]))
            con.commit()
        if st == "blocked":
            pending = con.execute("SELECT 1 FROM blockers WHERE task_id=? AND status='pending'",
                                  (r["task_id"],)).fetchone()
            if not pending:
                create_blocker(r["task_id"], context=f"Runner reports session "
                               f"{r['session_id']} blocked/waiting for input.")
                raised.append(r["task_id"])
    return raised


# --- decision persistence + resume ---------------------------------------------------------------

def _append(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def _task_log(run_id: str) -> pathlib.Path:
    log = VAULT / "coding-runs" / f"{run_id}-log.md"
    if not log.exists():
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("\n".join([
            "---", f"title: Task Log {run_id}", "type: task-log",
            f"permalink: coding-runs/{run_id}-log", "tags: [task-log]", "---", "",
            f"# Task Log - {run_id}", "",
        ]), encoding="utf-8")
    return log


def resolve(task_id: str, decision: str, rationale: str = "") -> dict:
    """Persist the human's decision everywhere it belongs; task -> resume-ready."""
    con = db()
    t = _task(con, task_id)
    b = con.execute("SELECT * FROM blockers WHERE task_id=?", (task_id,)).fetchone()
    if not b:
        sys.exit(f"no blocker recorded for {task_id}")
    run = con.execute("SELECT * FROM runs WHERE run_id=?", (t["run_id"],)).fetchone()
    stamp = NOW()

    # 1. blocker note: decision section + status flip
    report = pathlib.Path(b["report_path"])
    if report.exists():
        txt = report.read_text(encoding="utf-8")
        txt = txt.replace("status: pending", "status: resolved").replace(
            "tags: [blocker, escalation, pending]", "tags: [blocker, escalation, resolved]")
        txt += (f"\n## HUMAN DECISION ({stamp})\n\n> {decision}\n\n"
                + (f"Rationale: {rationale}\n" if rationale else ""))
        report.write_text(txt, encoding="utf-8")

    # 2. run task log
    _append(_task_log(t["run_id"]),
            f"\n- **{stamp}** `{task_id}` blocker resolved by human (via voice escalation): "
            f"{decision}" + (f" _(rationale: {rationale})_" if rationale else "") + "\n")

    # 3. project decisions page (human-readable register)
    decisions_page = VAULT / "decisions.md"
    if decisions_page.exists() and "## Blocker decisions" not in decisions_page.read_text(encoding="utf-8"):
        _append(decisions_page, "\n## Blocker decisions (voice escalations)\n\n")
    _append(decisions_page,
            f"- **{stamp}** [[Blocker - {task_id}]] - {decision}\n")

    # 4. the PRD / implementation note this task came from
    if run and run["prd_path"] and pathlib.Path(run["prd_path"]).exists():
        prd = pathlib.Path(run["prd_path"])
        if "## Escalation decisions" not in prd.read_text(encoding="utf-8"):
            _append(prd, "\n## Escalation decisions\n\n")
        _append(prd, f"- **{stamp}** task `{task_id}`: {decision}\n")

    # 5. state: blocked -> resume-ready
    con.execute("UPDATE blockers SET status='resolved', decision=?, rationale=?, decided_at=? "
                "WHERE task_id=?", (decision, rationale, stamp, task_id))
    con.execute("UPDATE tasks SET status='resume-ready', updated_at=? WHERE task_id=?",
                (stamp, task_id))
    con.commit()
    print(f"decision saved; {task_id} is resume-ready")
    return {"task_id": task_id, "status": "resume-ready", "report": str(report)}


def resume(task_id: str) -> dict:
    """Send the decision back to the coding agent. No merge, no deploy."""
    con = db()
    t = _task(con, task_id)
    b = con.execute("SELECT * FROM blockers WHERE task_id=?", (task_id,)).fetchone()
    if not b or b["status"] != "resolved":
        sys.exit(f"{task_id} has no resolved blocker (status={b['status'] if b else 'none'})")
    decision = b["decision"]
    message = (f"HUMAN DECISION on your blocker: {decision}. "
               f"Continue the task accordingly. Do not merge or push.")

    if t["session_id"] and not t["session_id"].startswith("sim-"):
        ao_runner._run(ao_runner._fmt(ao_runner.AO_CMDS["send"],
                                      sid=t["session_id"], msg=message))
        new_status = "running"
    else:
        # simulated session: leave a real, visible resume marker commit in the worktree
        wt = t["worktree"]
        if wt and pathlib.Path(wt).exists():
            marker = pathlib.Path(wt) / f"AO_RESUME_{task_id}.md"
            marker.write_text(f"# Resumed after human decision\n\n{message}\n", encoding="utf-8")
            subprocess.run(["git", "-C", wt, "add", "-A"], capture_output=True)
            subprocess.run(["git", "-C", wt, "-c", "user.email=ao-sim@local",
                            "-c", "user.name=ao-sim", "commit", "-m",
                            f"[sim] resume {task_id} with human decision"], capture_output=True)
        new_status = "completed"  # sim work finishes immediately after resume

    stamp = NOW()
    con.execute("UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                (new_status, stamp, task_id))
    con.execute("UPDATE blockers SET status='resumed', resumed_at=? WHERE task_id=?",
                (stamp, task_id))
    con.commit()
    report = pathlib.Path(b["report_path"] or "")
    if report.exists():
        txt = report.read_text(encoding="utf-8")
        txt = txt.replace("status: resolved", "status: resumed").replace(
            "tags: [blocker, escalation, resolved]", "tags: [blocker, escalation, resumed]")
        report.write_text(txt, encoding="utf-8")
    _append(_task_log(t["run_id"]),
            f"- **{stamp}** `{task_id}` resumed with the human decision "
            f"(mode: {'live ao send' if new_status == 'running' else 'simulated marker commit'}); "
            f"status -> {new_status}\n")
    print(f"{task_id} resumed -> {new_status}")
    return {"task_id": task_id, "status": new_status}


def resolve_and_resume(task_id: str, decision: str, rationale: str = "") -> dict:
    """One call for the voice brain: save decision everywhere, then resume the agent."""
    resolve(task_id, decision, rationale)
    return resume(task_id)


# --- queries (dashboard / voice brain) ------------------------------------------------------------

def list_blockers(include_resolved: bool = True) -> list[dict]:
    con = db()
    q = "SELECT b.*, t.text AS task_text, t.agent, t.worktree, t.status AS task_status " \
        "FROM blockers b JOIN tasks t ON t.task_id = b.task_id ORDER BY b.created_at DESC"
    rows = [dict(r) for r in con.execute(q)]
    if not include_resolved:
        rows = [r for r in rows if r["status"] == "pending"]
    return rows


def pending_blockers_full() -> list[dict]:
    """Pending blockers with full report text - what the voice brain reads aloud from."""
    out = []
    for r in list_blockers(include_resolved=False):
        p = pathlib.Path(r["report_path"] or "")
        r["report_markdown"] = p.read_text(encoding="utf-8") if p.exists() else "(report missing)"
        out.append(r)
    return out


# --- CLI -----------------------------------------------------------------------------------------

def cmd_list(_a) -> None:
    rows = list_blockers()
    if not rows:
        print("(no blockers)")
    for r in rows:
        print(f"  {r['status']:<9} {r['task_id']}  task={r['task_status']:<13} "
              f"decision={ (r['decision'] or '-')[:60] }")


def cmd_detect(_a) -> None:
    raised = detect()
    print(f"detected {len(raised)} new blocker(s): {raised or '-'}")


def cmd_watch(a) -> None:
    print(f"watching every {a.interval}s (Ctrl+C to stop); toast + dashboard on new blockers")
    while True:
        detect()
        time.sleep(a.interval)


def cmd_simulate(a) -> None:
    con = db()
    _task(con, a.task)
    con.execute("UPDATE tasks SET status='blocked', updated_at=? WHERE task_id=?", (NOW(), a.task))
    con.commit()
    create_blocker(a.task, a.context)


def cmd_resolve(a) -> None:
    resolve(a.task, a.decision, a.rationale or "")


def cmd_resume(a) -> None:
    resume(a.task)


def main() -> None:
    p = argparse.ArgumentParser(description="Blocker escalation (toast + voice; no phone/push).")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("detect").set_defaults(fn=cmd_detect)
    w = sub.add_parser("watch"); w.add_argument("--interval", type=int, default=15)
    w.set_defaults(fn=cmd_watch)
    s = sub.add_parser("simulate"); s.add_argument("--task", required=True)
    s.add_argument("--context", default="Agent asked for human input.")
    s.set_defaults(fn=cmd_simulate)
    r = sub.add_parser("resolve"); r.add_argument("--task", required=True)
    r.add_argument("--decision", required=True); r.add_argument("--rationale", default="")
    r.set_defaults(fn=cmd_resolve)
    u = sub.add_parser("resume"); u.add_argument("--task", required=True)
    u.set_defaults(fn=cmd_resume)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
