"""Optional Gmail and Calendar tools with local OAuth tokens."""
from __future__ import annotations

import base64
import datetime as dt
import email.message
import os
import pathlib

STATE_DIR = pathlib.Path(os.environ.get("AI_CTO_STATE", pathlib.Path.home() / ".ai-cto"))
TOKEN_PATH = pathlib.Path(os.environ.get("AI_CTO_GOOGLE_TOKEN", STATE_DIR / "google-token.json"))
CLIENT_SECRET = pathlib.Path(os.environ.get("AI_CTO_GOOGLE_CLIENT_SECRET", ".run/google-client-secret.json"))
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
]


def _missing(action: str, detail: str) -> dict:
    return {"ok": False, "action": action, "target": "", "summary": detail,
            "proof": "google_setup_checked", "error": detail}


def _service(api: str, version: str):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except Exception:
        raise RuntimeError("Google API libraries are not installed in this Python environment")

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CLIENT_SECRET.exists():
                raise RuntimeError(f"missing Google OAuth client secret: {CLIENT_SECRET}")
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return build(api, version, credentials=creds)


def google_auth_status() -> dict:
    return {"ok": True, "action": "google_auth_status", "target": str(TOKEN_PATH),
            "summary": "Google token present" if TOKEN_PATH.exists() else "Google token not configured",
            "proof": f"token_exists={TOKEN_PATH.exists()} client_secret_exists={CLIENT_SECRET.exists()}",
            "token_exists": TOKEN_PATH.exists(), "client_secret_exists": CLIENT_SECRET.exists()}


def email_search(query: str, limit: int = 10) -> dict:
    try:
        svc = _service("gmail", "v1")
    except Exception as e:
        return _missing("email_search", str(e))
    res = svc.users().messages().list(userId="me", q=query, maxResults=min(limit, 25)).execute()
    messages = res.get("messages", [])
    return {"ok": True, "action": "email_search", "target": query,
            "summary": f"Found {len(messages)} message(s)", "proof": f"messages={len(messages)}",
            "messages": messages}


def email_read_thread(thread_id: str) -> dict:
    try:
        svc = _service("gmail", "v1")
    except Exception as e:
        return _missing("email_read_thread", str(e))
    thread = svc.users().threads().get(userId="me", id=thread_id, format="metadata").execute()
    snippets = [m.get("snippet", "") for m in thread.get("messages", [])]
    return {"ok": True, "action": "email_read_thread", "target": thread_id,
            "summary": f"Read {len(snippets)} message(s)", "proof": f"messages={len(snippets)}",
            "snippets": snippets}


def email_draft_reply(to: str, subject: str, body: str) -> dict:
    try:
        svc = _service("gmail", "v1")
    except Exception as e:
        return _missing("email_draft_reply", str(e))
    msg = email.message.EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    draft = svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return {"ok": True, "action": "email_draft_reply", "target": draft.get("id", ""),
            "summary": f"Drafted email to {to}", "proof": f"draft_id={draft.get('id')}"}


def email_send_draft(draft_id: str, approved: bool = False) -> dict:
    if not approved:
        return {"ok": False, "action": "email_send_draft", "target": draft_id,
                "summary": "Explicit approval required before sending email",
                "proof": "approval_checked", "error": "approval required"}
    try:
        svc = _service("gmail", "v1")
    except Exception as e:
        return _missing("email_send_draft", str(e))
    sent = svc.users().drafts().send(userId="me", body={"id": draft_id}).execute()
    return {"ok": True, "action": "email_send_draft", "target": draft_id,
            "summary": "Sent draft", "proof": f"message_id={sent.get('id')}"}


def calendar_read(days: int = 7) -> dict:
    try:
        svc = _service("calendar", "v3")
    except Exception as e:
        return _missing("calendar_read", str(e))
    now = dt.datetime.now(dt.timezone.utc)
    end = now + dt.timedelta(days=max(1, min(days, 31)))
    res = svc.events().list(calendarId="primary", timeMin=now.isoformat(),
                            timeMax=end.isoformat(), singleEvents=True,
                            orderBy="startTime", maxResults=20).execute()
    events = res.get("items", [])
    slim = [{"id": e.get("id"), "summary": e.get("summary"), "start": e.get("start")}
            for e in events]
    return {"ok": True, "action": "calendar_read", "target": "primary",
            "summary": f"Read {len(slim)} event(s)", "proof": f"events={len(slim)}",
            "events": slim}


def calendar_create_event(summary: str, start_iso: str, end_iso: str, approved: bool = False) -> dict:
    if not approved:
        return {"ok": False, "action": "calendar_create_event", "target": summary,
                "summary": "Explicit approval required before creating calendar event",
                "proof": "approval_checked", "error": "approval required"}
    try:
        svc = _service("calendar", "v3")
    except Exception as e:
        return _missing("calendar_create_event", str(e))
    event = {"summary": summary, "start": {"dateTime": start_iso}, "end": {"dateTime": end_iso}}
    created = svc.events().insert(calendarId="primary", body=event).execute()
    return {"ok": True, "action": "calendar_create_event", "target": created.get("htmlLink", ""),
            "summary": f"Created event {summary}", "proof": f"event_id={created.get('id')}"}
