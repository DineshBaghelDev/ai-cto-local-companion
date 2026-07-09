#!/usr/bin/env python3
"""Small local CTO companion front door.

This is glue only. Memory stays Markdown in the vault, coding runs stay in
coder.py/ao_runner.py, and blocker handling stays in escalation.py.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys

import coder

ROOT = pathlib.Path(__file__).resolve().parents[1]
VAULT = pathlib.Path(os.environ.get("AI_CTO_VAULT", ROOT / "vault"))
DECISIONS = VAULT / "decisions"

REQUIRED = ("memory-engine.md", "coding-runner.md", "voice-stack.md")
FORBIDDEN = ("deepgram", "elevenlabs", "cartesia", "twilio", "daily.co", "anthropic_api_key", "openai_api_key")


def read(path: pathlib.Path) -> str:
    if not path.exists():
        sys.exit(f"Missing required decision: {path}. Stop and ask before proceeding.")
    return path.read_text(encoding="utf-8")


def decision_check() -> dict[str, str]:
    docs = {name: read(DECISIONS / name) for name in REQUIRED}
    if "Agent Orchestrator" in docs["coding-runner.md"] or "agent-orchestrator" in docs["coding-runner.md"]:
        ao = read(DECISIONS / "agent-orchestrator-repo.md")
        for needle in ("AgentWrapper/agent-orchestrator", "0.10.1-nightly", "249c67046d14809943d228b01eefedb10821e5dc"):
            if needle not in docs["coding-runner.md"] + ao:
                sys.exit(f"AO decision mismatch: expected {needle!r} in coding-runner/AO decision files.")
        docs["agent-orchestrator-repo.md"] = ao
    graphiti = DECISIONS / "graphiti-status.md"
    docs["graphiti-status.md"] = graphiti.read_text(encoding="utf-8") if graphiti.exists() else "Graphiti absent: V1 future work only."
    return docs


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "untitled"


def cmd_doctor(_args) -> None:
    decision_check()
    hits = []
    for p in [*ROOT.glob("*.py"), *ROOT.glob("scripts/*.py"), *ROOT.glob("voice/*.py")]:
        for line in p.read_text(encoding="utf-8", errors="ignore").lower().splitlines():
            if p.name == "cto.py" and "forbidden" in line:
                continue
            if any(ok in line for ok in ("no ", "excluded", "forbidden", "zero paid")):
                continue
            for word in FORBIDDEN:
                if word in line and re.search(rf"\b(import|from|pip|uv|npm|require|{re.escape(word)}[\._]|{re.escape(word.upper())})\b", line):
                    hits.append(f"{p.relative_to(ROOT)}: {word}")
    print("decisions: ok")
    print("paid/cloud dependency scan:", "ok" if not hits else "review " + "; ".join(hits))
    print("runner: AgentWrapper/agent-orchestrator via scripts/ao_runner.py")
    print("voice: Pipecat local stack from vault/decisions/voice-stack.md")
    print("github cli:", _gh_status())


def _gh_status() -> str:
    if shutil.which("gh"):
        return "native gh on PATH"
    try:
        p = subprocess.run(["wsl", "-e", "bash", "-lc", "command -v gh >/dev/null"],
                           text=True, capture_output=True, encoding="utf-8", timeout=10)
    except subprocess.TimeoutExpired:
        return "WSL check timed out"
    return "WSL gh available" if p.returncode == 0 else "not found"


def cmd_summary(args) -> None:
    decision_check()
    body = args.text or sys.stdin.read()
    subprocess.run([sys.executable, str(ROOT / "scripts" / "vault.py"), "summary", args.title, body], check=True)


def cmd_prd(args) -> None:
    decision_check()
    folder = VAULT / "prds"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{slug(args.title)}.md"
    today = dt.date.today().isoformat()
    tasks = args.task or ["Clarify implementation tasks before dispatch"]
    transcript = args.transcript or sys.stdin.read().strip() or "(no transcript supplied)"
    lines = [
        "---",
        f"title: {args.title}",
        "type: prd",
        "status: draft",
        f"permalink: prds/{slug(args.title)}",
        "tags: [prd]",
        "---",
        "",
        f"# PRD: {args.title}",
        "",
        f"- **Date:** {today}",
        "- **Source:** CTO companion text/voice summary",
        "",
        "## Conversation Summary",
        "",
        transcript,
        "",
        "## Goal",
        "",
        args.goal or "_Fill before approval._",
        "",
        "## Implementation Tasks",
        "",
    ]
    lines += [f"- [ ] {t}" for t in tasks]
    lines += ["", "## Relations", "- part_of [[Active Projects]]", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    print(path)


def cmd_approve(args) -> None:
    decision_check()
    path = pathlib.Path(args.prd)
    if not path.is_absolute():
        path = VAULT / path
    txt = read(path)
    path.write_text(re.sub(r"(?m)^status:\s*\w+\s*$", "status: approved", txt, count=1), encoding="utf-8")
    print(f"approved: {path}")


def run_script(name: str, extra: list[str]) -> None:
    decision_check()
    subprocess.run([sys.executable, str(ROOT / "scripts" / name), *extra], cwd=ROOT, check=True)


def cmd_dispatch(args) -> None:
    run_script("coder.py", ["dispatch", "--prd", args.prd, "--repo", args.repo, "--default-agent", args.default_agent])


def cmd_poll(args) -> None:
    run_script("coder.py", ["poll", *(["--run", args.run] if args.run else [])])


def cmd_collect(args) -> None:
    run_script("coder.py", ["collect", *(["--run", args.run] if args.run else [])])


def cmd_preview(args) -> None:
    run_script("coder.py", ["preview", "--run", args.run, "--cmd", args.cmd,
                            "--timeout", str(args.timeout)])


def cmd_feedback(args) -> None:
    run_script("coder.py", ["feedback", "--task", args.task, "--message", args.message])


def cmd_pr(args) -> None:
    extra = ["pr", "--task", args.task]
    if args.title:
        extra += ["--title", args.title]
    if args.body:
        extra += ["--body", args.body]
    run_script("coder.py", extra)


def cmd_merge_pr(args) -> None:
    extra = ["merge-pr", "--pr", args.pr, "--method", args.method]
    if args.repo:
        extra += ["--repo", args.repo]
    run_script("coder.py", extra)


def cmd_new_repo(args) -> None:
    decision_check()
    path = pathlib.Path(args.path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        subprocess.run(["git", "init", "-b", args.branch, str(path)], check=True)
    if args.readme:
        readme = path / "README.md"
        if not readme.exists():
            readme.write_text(f"# {path.name}\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(path), "-c", "user.email=ai-cto@local",
                            "-c", "user.name=ai-cto", "commit", "-m", "Initial commit"], check=True)
    if args.github:
        _gh(["repo", "create", args.github, "--source", str(path),
             "--remote", "origin", "--push", "--private" if args.private else "--public"], path)
    print(path)


def _gh(args: list[str], cwd: pathlib.Path) -> str:
    if shutil.which("gh"):
        p = subprocess.run(["gh", *args], cwd=cwd, text=True, capture_output=True, encoding="utf-8")
    else:
        wp = subprocess.run(["wsl", "wslpath", "-a", str(cwd)], text=True,
                            capture_output=True, encoding="utf-8")
        if wp.returncode != 0:
            sys.exit(wp.stderr.strip())
        fixed = []
        for a in args:
            if a == str(cwd):
                fixed.append(wp.stdout.strip())
            else:
                fixed.append(a)
        script = "cd " + shlex.quote(wp.stdout.strip()) + " && gh " + " ".join(shlex.quote(a) for a in fixed)
        p = subprocess.run(["wsl", "-e", "bash", "-lc", script], text=True,
                           capture_output=True, encoding="utf-8")
    if p.returncode != 0:
        sys.exit(p.stderr.strip() or p.stdout.strip())
    return p.stdout


def cmd_blockers(args) -> None:
    run_script("escalation.py", [args.action, *args.extra])


def cmd_status(_args) -> None:
    decision_check()
    snap = coder.status_snapshot()
    print(f"latest run: {snap['latest_run'] or '-'}")
    if snap["active"]:
        print("active:")
        for t in snap["active"]:
            print(f"  {t['status']:<10} {t['task_id']} {t['text'][:70]}")
    elif snap["tasks"]:
        print("active: none")
    else:
        print("tasks: none")
    print("recent activity:")
    for a in snap["activity"]:
        print(f"  {a['ts']} {a['kind']:<12} {a['message'][:90]}")


def cmd_start_task(args) -> None:
    decision_check()
    result = coder.start_task(args.repo, args.title, args.task, args.agent)
    if not result["ok"]:
        sys.exit(result["error"])
    print(f"started: {result['run_id']}")


def main() -> None:
    p = argparse.ArgumentParser(description="Local AI-CTO MVP glue.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    s = sub.add_parser("summary"); s.add_argument("title"); s.add_argument("text", nargs="?"); s.set_defaults(fn=cmd_summary)
    prd = sub.add_parser("prd"); prd.add_argument("--title", required=True); prd.add_argument("--goal")
    prd.add_argument("--transcript"); prd.add_argument("--task", action="append"); prd.set_defaults(fn=cmd_prd)
    a = sub.add_parser("approve"); a.add_argument("prd"); a.set_defaults(fn=cmd_approve)
    d = sub.add_parser("dispatch"); d.add_argument("--prd", required=True); d.add_argument("--repo", required=True)
    d.add_argument("--default-agent", choices=["claude", "codex"], default="claude"); d.set_defaults(fn=cmd_dispatch)
    st = sub.add_parser("start-task"); st.add_argument("--repo", required=True)
    st.add_argument("--title", required=True); st.add_argument("--task", required=True)
    st.add_argument("--agent", choices=["claude", "codex"], default="claude")
    st.set_defaults(fn=cmd_start_task)
    for name, fn in (("poll", cmd_poll), ("collect", cmd_collect)):
        x = sub.add_parser(name); x.add_argument("--run"); x.set_defaults(fn=fn)
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
    nr = sub.add_parser("new-repo"); nr.add_argument("--path", required=True)
    nr.add_argument("--branch", default="main"); nr.add_argument("--readme", action="store_true", default=True)
    nr.add_argument("--github"); nr.add_argument("--private", action="store_true")
    nr.set_defaults(fn=cmd_new_repo)
    b = sub.add_parser("blockers"); b.add_argument("action", choices=["list", "detect", "watch"])
    b.add_argument("extra", nargs=argparse.REMAINDER); b.set_defaults(fn=cmd_blockers)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
