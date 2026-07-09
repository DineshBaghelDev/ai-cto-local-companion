"""Tiny Jarvis V1 self-checks; no network or Basic Memory write."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
action_root = ROOT / ".run" / "test-jarvis-v1"
shutil.rmtree(action_root, ignore_errors=True)
action_root.mkdir(parents=True, exist_ok=True)
os.environ["AI_CTO_STATE"] = tmp.name
os.environ["AI_CTO_VAULT"] = str(ROOT / "vault")
os.environ["AI_CTO_PROJECT_ROOTS"] = str(action_root)
os.environ["AI_CTO_ALLOWED_ROOTS"] = str(action_root)

import coder  # noqa: E402
import jarvis  # noqa: E402
import supervisor  # noqa: E402
import browser_tools  # noqa: E402
import google_tools  # noqa: E402
import orchestrator  # noqa: E402

sys.path.insert(0, str(ROOT / "voice"))
import actions  # noqa: E402


def test_task_intent():
    assert jarvis.looks_like_task("add a --version flag")
    assert not jarvis.looks_like_task("what is the project status?")


def test_activity_log():
    coder.log_activity("test", "activity works")
    assert coder.recent_activity(1)[0]["message"] == "activity works"


def test_memory_command_shape():
    cmd = jarvis.memory_command("T")
    assert "tool" in cmd and "write-note" in cmd and "--project" in cmd


def test_html_extract():
    text = jarvis.extract_text("<html><script>bad()</script><h1>Hello</h1><p>World</p></html>")
    assert text == "Hello World"


def test_supervisor_ledger_and_proof():
    run_id = supervisor.start_run("test")
    result = supervisor.run_tool(
        "unit_tool", {"token": "secret"}, lambda token: {"ok": True, "summary": "ok", "proof": "unit"}, run_id
    )
    snap = supervisor.current_run_snapshot()
    assert result["ok"] and result["proof"] == "unit"
    assert snap["latest_run"]["id"] == run_id
    assert snap["steps"][0]["args_redacted"].find("secret") == -1


def test_supervisor_rejects_success_without_proof():
    result = supervisor.run_tool("bad_tool", {}, lambda: {"ok": True})
    assert not result["ok"]
    assert "proof" in result["error"]


def test_file_tools_guardrails():
    root = action_root
    text = root / "note.txt"
    written = actions.write_file(str(text), "hello")
    assert written["ok"] and written["proof"].startswith("file_exists")
    assert not actions.write_file(str(text), "again")["ok"]
    read = actions.read_file(str(text))
    assert read["ok"] and read["text"] == "hello"
    big = root / "big.txt"
    big.write_text("x" * 20, encoding="utf-8")
    assert not actions.read_file(str(big), max_bytes=5)["ok"]
    binary = root / "bin.dat"
    binary.write_bytes(b"a\x00b")
    assert not actions.read_file(str(binary))["ok"]
    assert not actions.run_command("git push", str(root))["ok"]


def test_v2_optional_tools_fail_closed():
    assert browser_tools.browser_read_page("https://example.com")["proof"] == "playwright_import_checked"
    assert google_tools.google_auth_status()["proof"].startswith("token_exists=")
    assert not google_tools.email_send_draft("draft-1")["ok"]
    assert not google_tools.calendar_create_event("Meet", "2026-07-10T10:00:00+05:30",
                                                  "2026-07-10T10:30:00+05:30")["ok"]
    assert "proof" in orchestrator.langgraph_status()


if __name__ == "__main__":
    test_task_intent()
    test_activity_log()
    test_memory_command_shape()
    test_html_extract()
    test_supervisor_ledger_and_proof()
    test_supervisor_rejects_success_without_proof()
    test_file_tools_guardrails()
    test_v2_optional_tools_fail_closed()
    shutil.rmtree(action_root, ignore_errors=True)
    tmp.cleanup()
    print("jarvis v1 self-check: ok")
