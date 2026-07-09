"""Tiny run ledger + proof contract for Jarvis tools."""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Callable

STATE_DIR = pathlib.Path(os.environ.get("AI_CTO_STATE", pathlib.Path.home() / ".ai-cto"))
DB_PATH = STATE_DIR / "supervisor.db"
NOW = lambda: datetime.datetime.now().isoformat(timespec="seconds")
SECRET_KEYS = ("key", "token", "secret", "password", "authorization")


class ApprovalRequired(Exception):
    def __init__(self, action: str, risk: str):
        super().__init__(f"approval required for {action}: {risk}")
        self.action = action
        self.risk = risk


@dataclass
class ToolResult:
    ok: bool
    action: str
    target: str
    summary: str
    proof: str
    error: str = ""

    def as_dict(self) -> dict:
        out = {
            "ok": self.ok,
            "action": self.action,
            "target": self.target,
            "summary": self.summary,
            "proof": self.proof,
        }
        if self.error:
            out["error"] = self.error
        return out


def db() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript("""
      CREATE TABLE IF NOT EXISTS runs(
        id TEXT PRIMARY KEY, user_request TEXT, status TEXT,
        started_at TEXT, completed_at TEXT, summary TEXT);
      CREATE TABLE IF NOT EXISTS steps(
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, tool TEXT,
        args_redacted TEXT, status TEXT, output_summary TEXT, proof TEXT,
        started_at TEXT, completed_at TEXT);
      CREATE TABLE IF NOT EXISTS artifacts(
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, kind TEXT,
        path_or_url TEXT, description TEXT);
      CREATE TABLE IF NOT EXISTS approvals(
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, action TEXT,
        risk TEXT, approved_by_user INTEGER, timestamp TEXT);
    """)
    return con


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[redacted]" if any(s in k.lower() for s in SECRET_KEYS) else _redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _json(value: Any) -> str:
    return json.dumps(_redact(value), ensure_ascii=True, default=str)[:4000]


def start_run(user_request: str = "") -> str:
    run_id = "run-" + datetime.date.today().isoformat() + "-" + uuid.uuid4().hex[:6]
    con = db()
    con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)",
                (run_id, user_request[:1000], "running", NOW(), None, ""))
    con.commit()
    return run_id


def _latest_open_run(con: sqlite3.Connection) -> str:
    row = con.execute(
        "SELECT id FROM runs WHERE status='running' ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    return row["id"] if row else start_run("voice/tool call")


def _normalize(tool: str, result: Any) -> dict:
    if not isinstance(result, dict):
        result = {"ok": True, "summary": str(result)}
    ok = bool(result.get("ok", True))
    target = str(result.get("target") or result.get("path") or result.get("dir")
                 or result.get("url") or result.get("run_id") or result.get("task_id")
                 or result.get("title") or "")
    summary = str(result.get("summary") or result.get("note") or result.get("error")
                  or result.get("title") or ("ok" if ok else "failed"))
    proof = str(result.get("proof") or "")
    if ok and not proof:
        if target:
            proof = f"target={target}"
        elif result.get("context") or result.get("results"):
            proof = "result_returned"
    normalized = dict(result)
    normalized.update({
        "ok": ok,
        "action": str(result.get("action") or tool),
        "target": target,
        "summary": summary[:1500],
        "proof": proof[:1500],
    })
    if ok and not proof:
        normalized["ok"] = False
        normalized["error"] = "tool returned success without proof"
        normalized["summary"] = normalized["error"]
    return normalized


def run_tool(tool_name: str, args: dict, fn: Callable[..., Any], run_id: str | None = None) -> dict:
    con = db()
    run_id = run_id or _latest_open_run(con)
    started = NOW()
    cur = con.execute(
        "INSERT INTO steps(run_id, tool, args_redacted, status, output_summary, proof, started_at, completed_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (run_id, tool_name, _json(args), "running", "", "", started, None),
    )
    step_id = cur.lastrowid
    con.commit()
    try:
        result = _normalize(tool_name, fn(**args))
        status = "completed" if result["ok"] else "failed"
    except ApprovalRequired as e:
        result = ToolResult(False, tool_name, e.action, e.risk, "approval_required", str(e)).as_dict()
        status = "approval-required"
        con.execute("INSERT INTO approvals(run_id, action, risk, approved_by_user, timestamp) VALUES (?,?,?,?,?)",
                    (run_id, e.action, e.risk, 0, NOW()))
    except Exception as e:  # tool failure must still be ledgered
        result = ToolResult(False, tool_name, "", str(e), "exception", str(e)).as_dict()
        status = "failed"
    con.execute(
        "UPDATE steps SET status=?, output_summary=?, proof=?, completed_at=? WHERE id=?",
        (status, result.get("summary", "")[:1500], result.get("proof", "")[:1500], NOW(), step_id),
    )
    if result.get("target") and status == "completed":
        con.execute("INSERT INTO artifacts(run_id, kind, path_or_url, description) VALUES (?,?,?,?)",
                    (run_id, result.get("action", tool_name), result["target"], result.get("summary", "")))
    con.commit()
    result["run_id"] = run_id
    result["step_id"] = step_id
    return result


def current_run_snapshot(limit: int = 8) -> dict:
    con = db()
    run = con.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    steps = []
    if run:
        rows = con.execute(
            "SELECT * FROM steps WHERE run_id=? ORDER BY id DESC LIMIT ?", (run["id"], limit)
        ).fetchall()
        steps = [dict(r) for r in rows]
    return {"latest_run": dict(run) if run else None, "steps": steps}


def cancel_current_task() -> dict:
    return {
        "ok": False,
        "action": "cancel_current_task",
        "target": "",
        "summary": "No Jarvis-owned cancellable process is running",
        "proof": "no_owned_processes",
        "error": "no active cancellable task",
    }


def _self_check() -> None:
    run_id = start_run("self-check")
    ok = run_tool("demo", {"x": 1}, lambda x: {"ok": True, "summary": "worked", "proof": f"x={x}"}, run_id)
    assert ok["ok"] and ok["proof"] == "x=1"
    bad = run_tool("bad", {}, lambda: {"ok": True}, run_id)
    assert not bad["ok"] and "without proof" in bad["error"]
    snap = current_run_snapshot()
    assert snap["latest_run"]["id"] == run_id and len(snap["steps"]) >= 2


if __name__ == "__main__":
    _self_check()
    print("supervisor self-check: ok")
