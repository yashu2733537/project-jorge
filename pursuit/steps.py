"""Step executors — map workflow DSL tools onto jorge's capabilities.

Each handler receives (env, params) where params has already been rendered
(templates resolved against execution state). Handlers return a StepResult of
(ok: bool, output: str, error: str).
"""
from __future__ import annotations

import json
import subprocess
import time
from typing import Any

import requests

import assistant as J


class StepResult:
    __slots__ = ("ok", "output", "error")

    def __init__(self, ok: bool, output: str = "", error: str = ""):
        self.ok = ok
        self.output = output
        self.error = error


def _timeout(params: dict[str, Any], default: int = 300) -> int:
    try:
        return int(params.get("timeout", default))
    except (TypeError, ValueError):
        return default


def run_step(env: dict[str, str], step: dict[str, Any],
             params: dict[str, Any]) -> StepResult:
    tool = step.get("tool", "")
    handler = {
        "shell": _shell,
        "read": _read,
        "write": _write,
        "append": _append,
        "list": _list,
        "web": _web,
        "research": _research,
        "email": _email,
        "notify": _notify,
        "http": _http,
        "webhook": _http,
        "set": _set,
        "expr": _expr,
        "sleep": _sleep,
        "delegate": _delegate,
        "memory": _memory,
    }.get(tool)
    if handler is None:
        return StepResult(False, "", f"unknown tool: {tool}")
    try:
        return handler(env, params)
    except subprocess.TimeoutExpired:
        return StepResult(False, "", f"step timed out after {_timeout(params)}s")
    except Exception as e:
        return StepResult(False, "", f"{type(e).__name__}: {e}")


def _shell(env: dict[str, str], params: dict[str, Any]) -> StepResult:
    command = str(params.get("command") or "").strip()
    if not command:
        return StepResult(False, "", "no command given")
    out = J.run_shell(command, _timeout(params))
    failed = out.lstrip().startswith("✗")
    return StepResult(not failed, out, out if failed else "")


def _read(env: dict[str, str], params: dict[str, Any]) -> StepResult:
    from pathlib import Path
    path = Path(str(params.get("path") or "")).expanduser()
    if not path.exists():
        return StepResult(False, "", f"file not found: {path}")
    content = path.read_text(encoding="utf-8", errors="replace")
    return StepResult(True, content)


def _write(env: dict[str, str], params: dict[str, Any]) -> StepResult:
    from pathlib import Path
    path = Path(str(params.get("path") or "")).expanduser()
    content = str(params.get("content") or "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return StepResult(True, f"wrote {len(content)} chars to {path}")


def _append(env: dict[str, str], params: dict[str, Any]) -> StepResult:
    from pathlib import Path
    path = Path(str(params.get("path") or "")).expanduser()
    content = str(params.get("content") or "")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(content)
    return StepResult(True, f"appended {len(content)} chars to {path}")


def _list(env: dict[str, str], params: dict[str, Any]) -> StepResult:
    from pathlib import Path
    path = Path(str(params.get("path") or ".")).expanduser()
    if not path.is_dir():
        return StepResult(False, "", f"not a directory: {path}")
    names = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
    return StepResult(True, "\n".join(names) if names else "(empty folder)")


def _web(env: dict[str, str], params: dict[str, Any]) -> StepResult:
    query = str(params.get("query") or "").strip()
    if not query:
        return StepResult(False, "", "no query given")
    n = int(params.get("n", 5) or 5)
    results = J.web_search(query, n)
    if not results:
        return StepResult(False, "", "search returned nothing (backend may be rate-limited)")
    lines = [f"{i}. {r['title']} — {r['url']}\n   {r.get('snippet', '')}" for i, r in enumerate(results, 1)]
    return StepResult(True, "\n".join(lines), "")


def _research(env: dict[str, str], params: dict[str, Any]) -> StepResult:
    query = str(params.get("query") or "").strip()
    if not query:
        return StepResult(False, "", "no query given")
    n = int(params.get("n", 8) or 8)
    results = J.web_research(query, n)
    lines = [f"{i}. {r['title']} — {r['url']}\n   {r.get('snippet', '')}" for i, r in enumerate(results, 1)]
    return StepResult(True, "\n".join(lines) if lines else "(no results)", "")


def _email(env: dict[str, str], params: dict[str, Any]) -> StepResult:
    to = str(params.get("to") or "").strip()
    subject = str(params.get("subject") or "(no subject)")
    if not to:
        return StepResult(False, "", "email step needs `to`")
    missing = [k for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_APP_PASSWORD") if not env.get(k)]
    if missing:
        return StepResult(False, "", f"email step needs SMTP_* in .env (missing: {', '.join(missing)})")
    body = params.get("body")
    if body is None:
        topic = str(params.get("topic") or "").strip()
        if not topic:
            return StepResult(False, "", "email step needs `body` or `topic`")
        draft, _usage = J.draft_email(env, to, subject, topic, str(params.get("tone") or "professional"))
        body = draft or ""
    J.send_email(env, to, subject, str(body))
    return StepResult(True, f"sent email to {to}: {subject}")


def _notify(env: dict[str, str], params: dict[str, Any]) -> StepResult:
    from .notify import send_notification
    channel = str(params.get("channel") or "ntfy")
    title = str(params.get("title") or "jorge pursuit")
    message = str(params.get("message") or "")
    config = params.get("config") if isinstance(params.get("config"), dict) else {}
    if "," in channel:
        ok_all, parts = True, []
        for ch in channel.split(","):
            ok, detail = send_notification(env, ch.strip(), title, message, config=config)
            ok_all = ok_all and ok
            parts.append(f"{ch.strip()}: {detail}")
        return StepResult(ok_all, "\n".join(parts), "" if ok_all else "; ".join(parts))
    ok, detail = send_notification(env, channel, title, message, config=config)
    return StepResult(ok, detail, "" if ok else detail)


def _http(env: dict[str, str], params: dict[str, Any]) -> StepResult:
    url = str(params.get("url") or "").strip()
    if not url:
        return StepResult(False, "", "http step needs `url`")
    method = str(params.get("method") or "get").upper()
    headers = params.get("headers") if isinstance(params.get("headers"), dict) else {}
    body = params.get("json") or params.get("data") or params.get("body")
    timeout = _timeout(params)
    r = requests.request(method, url, headers=headers, json=body if params.get("json") is not None else None,
                         data=body if params.get("json") is None else None, timeout=timeout)
    snippet = r.text[:2000]
    ok = 200 <= r.status_code < 400
    return StepResult(ok, f"HTTP {r.status_code} {r.reason}\n{snippet}", "" if ok else f"HTTP {r.status_code} {r.reason}")


def _set(env: dict[str, str], params: dict[str, Any]) -> StepResult:
    name = str(params.get("name") or "").strip()
    if not name:
        return StepResult(False, "", "set step needs `name`")
    value = params.get("value")
    return StepResult(True, json.dumps(value) if isinstance(value, (dict, list)) else str(value), "")


def _expr(env: dict[str, str], params: dict[str, Any]) -> StepResult:
    from .schema import safe_eval
    name = str(params.get("name") or "").strip()
    if not name:
        return StepResult(False, "", "expr step needs `name`")
    try:
        value = safe_eval(params.get("value"), {})
    except ValueError as e:
        return StepResult(False, "", str(e))
    return StepResult(True, json.dumps(value) if isinstance(value, (dict, list)) else str(value), "")


def _sleep(env: dict[str, str], params: dict[str, Any]) -> StepResult:
    seconds = float(params.get("seconds", params.get("timeout", 0)) or 0)
    if seconds > 0:
        time.sleep(seconds)
    return StepResult(True, f"slept {seconds}s")


def _delegate(env: dict[str, str], params: dict[str, Any]) -> StepResult:
    task = str(params.get("task") or "").strip()
    if not task:
        return StepResult(False, "", "delegate step needs `task`")
    workdir = str(params.get("dir") or "")
    out = J.summarize_delegate(J.run_delegate(task, workdir))
    failed = "✗" in out[:8] or "ERROR" in out[:200].upper()
    return StepResult(not failed, out, out if failed else "")


def _memory(env: dict[str, str], params: dict[str, Any]) -> StepResult:
    action = str(params.get("action") or "add")
    if action == "add":
        text = str(params.get("text") or "").strip()
        if not text:
            return StepResult(False, "", "memory add needs `text`")
        J.save_memory_entry(text)
        return StepResult(True, "remembered.")
    if action == "list":
        notes = J.load_memory()
        return StepResult(True, "\n- " + "\n- ".join(notes) if notes else "(no notes)")
    if action == "clear":
        J.forget_memory()
        return StepResult(True, "memory cleared.")
    return StepResult(False, "", f"unknown memory action: {action}")