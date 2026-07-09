#!/usr/bin/env python3
"""vault.py - thin convenience wrapper over Basic Memory for the AI-CTO vault.

Basic Memory already does the heavy lifting (storage, search, backlinks, MCP).
This wrapper only encodes *conventions* so every session writes memory the same way:

  session summaries  -> folder `sessions/`,   type `session`
  decisions          -> folder `decisions/`,  one file per key (machine-checkable)
  project context    -> search + recent activity
  open loops         -> unchecked `- [ ]` items in open-loops.md

It shells out to the `basic-memory` CLI; it does not reimplement any storage.

Usage:
  python vault.py summary "<title>" ["<markdown>"]      # omit body to read stdin
  python vault.py decide <key> "<one-line decision>" [--status decided|deferred]
  python vault.py context ["<query>"]                   # no query -> recent activity
  python vault.py loops                                 # list open loops
  python vault.py decisions                             # list decided keys (register)

Project defaults to `ai-cto` (override with env BASIC_MEMORY_PROJECT).
"""
from __future__ import annotations
import os, sys, shutil, subprocess, datetime, pathlib

PROJECT = os.environ.get("BASIC_MEMORY_PROJECT", "ai-cto")
VAULT = pathlib.Path(os.environ.get("AI_CTO_VAULT", r"E:\Projects\AI CTO\vault"))
TODAY = datetime.date.today().isoformat()


def bm_exe() -> str:
    exe = shutil.which("basic-memory") or shutil.which("bm")
    if exe:
        return exe
    fallback = pathlib.Path.home() / "AppData/Roaming/uv/tools/basic-memory/Scripts/basic-memory.exe"
    if fallback.exists():
        return str(fallback)
    sys.exit("basic-memory CLI not found on PATH. Run: uv tool install basic-memory")


def bm(*args: str, stdin: str | None = None) -> str:
    r = subprocess.run([bm_exe(), *args], input=stdin, text=True,
                       capture_output=True, encoding="utf-8")
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        sys.exit(r.returncode)
    return r.stdout


def write_note(title: str, folder: str, content: str, ntype: str = "note",
               tags: str | None = None, overwrite: bool = False) -> None:
    args = ["tool", "write-note", "--title", title, "--folder", folder,
            "--type", ntype, "--project", PROJECT]
    if tags:
        args += ["--tags", tags]
    if overwrite:
        args += ["--overwrite"]
    print(bm(*args, stdin=content))


def cmd_summary(argv: list[str]) -> None:
    if not argv:
        sys.exit('usage: vault.py summary "<title>" ["<markdown>"]')
    title = argv[0]
    body = argv[1] if len(argv) > 1 else sys.stdin.read()
    content = (f"# {title}\n\n"
               f"- **Date:** {TODAY}\n- **Type:** session summary\n\n"
               f"{body.strip()}\n\n## Relations\n- part_of [[Index]]\n")
    write_note(f"Session {TODAY} - {title}", "sessions", content,
               ntype="session", tags="session")


def cmd_decide(argv: list[str]) -> None:
    status = "decided"
    if "--status" in argv:
        i = argv.index("--status"); status = argv[i + 1]; del argv[i:i + 2]
    if len(argv) < 2:
        sys.exit('usage: vault.py decide <key> "<one-line decision>" [--status decided|deferred]')
    key, decision = argv[0], argv[1]
    badge = "✅ Decided" if status == "decided" else "⏸️ Deferred"
    title = "Decision - " + key.replace("-", " ").title()
    content = (f"# {title}\n\n"
               f"- **Decision key:** `{key}`\n- **Status:** {badge}\n- **Date:** {TODAY}\n\n"
               f"## Decision\n\n{decision}\n\n## Rationale\n\n_TODO: fill in._\n\n"
               f"## Pinned\n\n| Field | Value |\n|---|---|\n| Repo | _TODO_ |\n\n"
               f"## Relations\n- part_of [[Decisions]]\n")
    write_note(title, "decisions", content, ntype="decision",
               tags=f"decision,{status}", overwrite=True)
    print(f"Recorded decision '{key}' ({status}). Remember to add it to decisions.md register.")


def cmd_context(argv: list[str]) -> None:
    if argv:
        print(bm("tool", "search-notes", argv[0], "--project", PROJECT))
    else:
        print(bm("tool", "recent-activity", "--project", PROJECT))


def cmd_loops(_: list[str]) -> None:
    f = VAULT / "open-loops.md"
    if not f.exists():
        sys.exit(f"not found: {f}")
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("- [ ]"):
            print(line.strip())


def cmd_decisions(_: list[str]) -> None:
    d = VAULT / "decisions"
    if not d.exists():
        sys.exit(f"not found: {d}")
    keys = sorted(p.stem for p in d.glob("*.md"))
    print("Decided keys (a file's existence == decision made):")
    for k in keys:
        print(f"  - {k}")


COMMANDS = {"summary": cmd_summary, "decide": cmd_decide, "context": cmd_context,
            "loops": cmd_loops, "decisions": cmd_decisions}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
