"""Notification hooks for workflow events: email, ntfy.sh, gotify.

Channels are configured per workflow/task in the "notify" section of the
workflow definition (or passed at enqueue time):

{
  "notify": {
    "email":  {"on": ["start", "success", "error"], "to": "boss@example.com"},
    "ntfy":   {"on": ["error"], "topic": "jorge"},
    "gotify": {"on": ["success", "error"], "priority": 8}
  }
}

Each channel's "on" list selects the events it fires for. Server/token/url
defaults come from .env (NTFY_SERVER, NTFY_TOPIC, NTFY_TOKEN, GOTIFY_URL,
GOTIFY_TOKEN, NOTIFY_EMAIL_TO). Missing configuration is skipped, never fatal.
"""
from __future__ import annotations

import os
from typing import Any

import requests

EVENTS = ("start", "success", "error", "retry")


def _env() -> dict[str, str]:
    import assistant as J
    return J.load_env()


def send_notification(env: dict[str, str], channel: str, title: str, message: str,
                      task: dict[str, Any] | None = None,
                      config: dict[str, Any] | None = None) -> tuple[bool, str]:
    handler = {
        "email": _email,
        "ntfy": _ntfy,
        "gotify": _gotify,
    }.get(channel)
    if handler is None:
        return False, f"unknown channel: {channel}"
    try:
        return handler(env, title, message, task or {}, config or {})
    except Exception as e:  # notifications must never break the worker
        return False, f"{type(e).__name__}: {e}"


def _email(env: dict[str, str], title: str, message: str, task: dict[str, Any],
           config: dict[str, Any]) -> tuple[bool, str]:
    to = config.get("to") or env.get("NOTIFY_EMAIL_TO")
    if not to:
        return False, "email notification needs `to` or NOTIFY_EMAIL_TO in .env"
    missing = [k for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_APP_PASSWORD") if not env.get(k)]
    if missing:
        return False, f"email notification needs SMTP_* in .env (missing: {', '.join(missing)})"
    import assistant as J
    J.send_email(env, to, title, message)
    return True, f"sent to {to}"


def _ntfy(env: dict[str, str], title: str, message: str, task: dict[str, Any],
          config: dict[str, Any]) -> tuple[bool, str]:
    topic = config.get("topic") or env.get("NTFY_TOPIC")
    if not topic:
        return False, "ntfy notification needs `topic` or NTFY_TOPIC in .env"
    server = (config.get("server") or env.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    token = config.get("token") or env.get("NTFY_TOKEN")
    headers = {"Title": title[:255]}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.post(f"{server}/{topic}", data=message.encode("utf-8"), headers=headers, timeout=30)
    r.raise_for_status()
    return True, f"posted to {server}/{topic}"


def _gotify(env: dict[str, str], title: str, message: str, task: dict[str, Any],
            config: dict[str, Any]) -> tuple[bool, str]:
    url = (config.get("url") or env.get("GOTIFY_URL") or "").rstrip("/")
    token = config.get("token") or env.get("GOTIFY_TOKEN")
    if not url or not token:
        return False, "gotify notification needs `url` + `token` (or GOTIFY_URL/GOTIFY_TOKEN in .env)"
    payload = {"title": title[:255], "message": message, "priority": int(config.get("priority", 5))}
    r = requests.post(
        f"{url}/message?token={token}",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    return True, f"pushed to {url}"


def notify_event(env: dict[str, str], event: str, title: str, message: str,
                 task: dict[str, Any] | None = None,
                 db_path: str | None = None) -> list[dict[str, Any]]:
    """Send an event notification to every configured channel. Returns results."""
    config = (task or {}).get("notify") or {}
    results = []
    for channel, cfg in config.items():
        if not isinstance(cfg, dict):
            continue
        on = cfg.get("on") or list(EVENTS)
        if event not in on:
            continue
        ok, detail = send_notification(env, channel, title, message, task, cfg)
        results.append({"channel": channel, "event": event, "ok": ok, "detail": detail})
    if results and db_path:
        _log_notifications(task, event, results, db_path)
    return results


def _log_notifications(task: dict[str, Any] | None, event: str,
                       results: list[dict[str, Any]], db_path: str) -> None:
    if not task:
        return
    try:
        from .store import connect
        with connect(db_path) as conn:
            for r in results:
                conn.execute(
                    "INSERT INTO notifications (task_id, channel, event, ok, detail, sent_at)"
                    " VALUES (?, ?, ?, ?, ?, datetime('now'))",
                    (task.get("id"), r["channel"], r["event"], int(r["ok"]), r["detail"]),
                )
    except Exception:
        pass