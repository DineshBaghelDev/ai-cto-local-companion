"""Optional browser automation tools.

Uses Playwright when installed; otherwise falls back to safe open/list behavior.
"""
from __future__ import annotations

import json
import subprocess
import urllib.request
import webbrowser

DEVTOOLS = "http://127.0.0.1:9222/json"


def _missing_playwright(action: str) -> dict:
    return {
        "ok": False,
        "action": action,
        "target": "",
        "summary": "Playwright is not installed in this Python environment",
        "proof": "playwright_import_checked",
        "error": "install playwright and run `python -m playwright install chromium`",
    }


def browser_open_tab(url: str) -> dict:
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "action": "browser_open_tab", "target": url,
                "summary": "URL must start with http:// or https://", "proof": "scheme_checked",
                "error": "invalid URL"}
    webbrowser.open(url)
    return {"ok": True, "action": "browser_open_tab", "target": url,
            "summary": f"Opened {url}", "proof": "browser_open_called"}


def browser_list_tabs() -> dict:
    try:
        with urllib.request.urlopen(DEVTOOLS, timeout=2) as r:
            tabs = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "action": "browser_list_tabs", "target": DEVTOOLS,
                "summary": "Chrome DevTools is not reachable",
                "proof": "devtools_probe_failed",
                "error": f"start Chrome with --remote-debugging-port=9222 ({e})"}
    clean = [{"title": t.get("title", ""), "url": t.get("url", "")} for t in tabs]
    return {"ok": True, "action": "browser_list_tabs", "target": DEVTOOLS,
            "summary": f"Found {len(clean)} tab(s)", "proof": f"tabs={len(clean)}", "tabs": clean}


def browser_read_page(url: str = "") -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return _missing_playwright("browser_read_page")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url or "about:blank", wait_until="domcontentloaded", timeout=30000)
        text = page.locator("body").inner_text(timeout=10000)[:8000]
        final_url = page.url
        title = page.title()
        browser.close()
    return {"ok": True, "action": "browser_read_page", "target": final_url,
            "summary": title or final_url, "proof": f"loaded_url={final_url}", "text": text}


def browser_click(url: str, selector: str) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return _missing_playwright("browser_click")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.click(selector, timeout=10000)
        final_url = page.url
        title = page.title()
        browser.close()
    return {"ok": True, "action": "browser_click", "target": final_url,
            "summary": f"Clicked {selector} on {title or url}", "proof": f"selector_clicked={selector}"}


def browser_download(url: str, target: str = "") -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return _missing_playwright("browser_download")
    if not target:
        return {"ok": False, "action": "browser_download", "target": url,
                "summary": "target path is required", "proof": "target_checked", "error": "missing target"}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(accept_downloads=True)
        with page.expect_download(timeout=30000) as dl:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        download = dl.value
        download.save_as(target)
        browser.close()
    return {"ok": True, "action": "browser_download", "target": target,
            "summary": f"Downloaded {url}", "proof": f"download_saved={target}"}


def open_controlled_chrome(url: str = "about:blank") -> dict:
    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    try:
        subprocess.Popen([chrome, "--remote-debugging-port=9222", "--user-data-dir=%TEMP%\\jarvis-chrome", url])
    except Exception as e:
        return {"ok": False, "action": "open_controlled_chrome", "target": chrome,
                "summary": str(e), "proof": "process_start_failed", "error": str(e)}
    return {"ok": True, "action": "open_controlled_chrome", "target": url,
            "summary": "Started Chrome with DevTools on port 9222", "proof": "process_started"}
