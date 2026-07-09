"""Small shared helpers for Jarvis V1 glue.

Memory is Basic Memory CLI, tasks stay in coder.py. Web is urllib by default, or
Firecrawl (https://firecrawl.dev) when FIRECRAWL_API_KEY is set — cleaner markdown
extraction and JS-page handling, free tier ~500 credits/mo. Falls back to the plain
urllib/DuckDuckGo path on any Firecrawl error so web tools never go fully dark.
"""
from __future__ import annotations

import datetime
import html
import json
import os
import pathlib
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from html.parser import HTMLParser

import coder

PROJECT = os.environ.get("BASIC_MEMORY_PROJECT", "ai-cto")
ROOT = pathlib.Path(__file__).resolve().parents[1]
VAULT = pathlib.Path(os.environ.get("AI_CTO_VAULT", ROOT / "vault"))
URL_RE = re.compile(r"https?://\S+")
TASK_RE = re.compile(r"\b(add|build|create|fix|implement|change|write|update|make)\b", re.I)
REMEMBER_RE = re.compile(r"^\s*(remember|save|note)\b(?:\s+that)?\s*", re.I)

FIRECRAWL_BASE = "https://api.firecrawl.dev/v1"

# DuckDuckGo's /html/ endpoint silently serves its lite homepage (zero results,
# no error) instead of search results for non-browser User-Agents; a plain
# browser UA string is required to get real results back.
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


class _TextHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data.strip())


def extract_text(html_text: str, limit: int = 3000) -> str:
    parser = _TextHTMLParser()
    parser.feed(html_text)
    text = html.unescape(" ".join(parser.parts))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def memory_command(title: str, folder: str = "voice-notes") -> list[str]:
    bm = shutil.which("basic-memory") or str(
        pathlib.Path.home() / "AppData/Roaming/uv/tools/basic-memory/Scripts/basic-memory.exe"
    )
    return [bm, "tool", "write-note", "--title", title, "--folder", folder, "--project", PROJECT]


def remember_note(text: str, source: str = "jarvis") -> dict:
    now = datetime.datetime.now()
    title = f"Voice note {now:%Y-%m-%d %H:%M:%S}"
    body = (f"# {title}\n\n- **Date:** {now:%Y-%m-%d %H:%M}\n"
            f"- **Source:** {source}\n\n{text.strip()}\n\n"
            "## Relations\n- part_of [[Index]]\n")
    proc = subprocess.run(memory_command(title), input=body, text=True, capture_output=True,
                          encoding="utf-8", errors="replace")
    ok = proc.returncode == 0
    coder.log_activity("memory", f"{'saved' if ok else 'failed'}: {text[:100]}")
    return {"ok": ok, "title": title, "error": proc.stderr[-300:] if not ok else ""}


def tiny_memory_context(query: str, limit: int = 1800) -> str:
    hits: list[str] = []
    rg = shutil.which("rg")
    if rg and query.strip():
        proc = subprocess.run([rg, "-n", "-i", "--glob", "*.md", query[:80], str(VAULT)],
                              text=True, capture_output=True, encoding="utf-8",
                              errors="replace", timeout=10)
        hits = (proc.stdout or "").splitlines()[:5]
    activity = [f"{a['ts']} {a['kind']}: {a['message']}" for a in coder.recent_activity(5)]
    ctx = "\n".join(["Recent activity:", *activity, "", "Vault hits:", *hits])
    return ctx[:limit]


def _firecrawl_key() -> str:
    return os.environ.get("FIRECRAWL_API_KEY", "").strip()


def _firecrawl_post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{FIRECRAWL_BASE}/{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_firecrawl_key()}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _web_fetch_urllib(url: str, limit: int) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read(500_000).decode("utf-8", errors="replace")
    return {"url": url, "text": extract_text(raw, limit)}


def web_fetch(url: str, limit: int = 3000) -> dict:
    if _firecrawl_key():
        try:
            data = _firecrawl_post("scrape", {"url": url, "formats": ["markdown"]})
            text = ((data.get("data") or {}).get("markdown") or "").strip()
            if text:
                coder.log_activity("web-fetch", f"{url} (firecrawl)")
                return {"url": url, "text": text[:limit]}
        except Exception as e:
            coder.log_activity("web-fetch", f"firecrawl failed, falling back: {e}")
    result = _web_fetch_urllib(url, limit)
    coder.log_activity("web-fetch", url)
    return result


_JUNK_HOST_RE = re.compile(
    r"(?:^|\.)duckduckgo\.com$|(?:^|\.)duckduckgo\.gg$", re.I
)


def _web_search_duckduckgo(query: str, limit: int) -> dict:
    url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read(500_000).decode("utf-8", errors="replace")
    # Only trust DuckDuckGo's own result-redirect links (uddg=<encoded target>).
    # A blind "any http(s) URL on the page" fallback used to run when this
    # regex found nothing (e.g. DDG served a changed/blocked markup variant),
    # which surfaced DDG's own tracking/asset/ad links as "results" — junk.
    # If DDG's markup changes enough that this stops matching, return no
    # results rather than guessing.
    matches = re.findall(r'/l/\?uddg=([^&"\']+)', raw)
    seen: list[str] = []
    for m in matches:
        clean = urllib.parse.unquote(m).split("&")[0]
        try:
            host = urllib.parse.urlparse(clean).hostname or ""
        except ValueError:
            continue
        if not clean.startswith(("http://", "https://")):
            continue
        if _JUNK_HOST_RE.search(host):
            continue
        if clean not in seen:
            seen.append(clean)
        if len(seen) >= limit:
            break
    return {"query": query, "results": seen, "source": url}


def web_search(query: str, limit: int = 5) -> dict:
    if _firecrawl_key():
        try:
            data = _firecrawl_post("search", {"query": query, "limit": limit})
            results = [item["url"] for item in (data.get("data") or []) if item.get("url")]
            if results:
                coder.log_activity("web-search", f"{query} (firecrawl)")
                return {"query": query, "results": results[:limit], "source": "firecrawl"}
        except Exception as e:
            coder.log_activity("web-search", f"firecrawl failed, falling back: {e}")
    result = _web_search_duckduckgo(query, limit)
    coder.log_activity("web-search", query)
    return result


def remember_text(message: str) -> str | None:
    m = REMEMBER_RE.match(message)
    return message[m.end():].strip() if m else None


def looks_like_task(message: str) -> bool:
    q = message.strip().lower()
    return bool(TASK_RE.search(q)) and not q.startswith(("what ", "why ", "how ", "when "))


def first_url(message: str) -> str | None:
    m = URL_RE.search(message)
    return m.group(0).rstrip(").,") if m else None
