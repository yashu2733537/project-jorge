#!/usr/bin/env python3
"""jorge — your personal AI assistant: emails, web search, file sorting, memory.

Examples:
  jorge email --to friend@example.com --subject "Weekend" --topic "ask when they're free saturday"
  jorge web "best free vps 2026" --ask
  jorge sort ~/Downloads --dry-run
  jorge remember "Yash likes building games in Godot"

Runs standalone as a CLI, or as a JSON-line bridge (JORGE_BOT=1) driven by
whatsapp-bot/bot.js through bridge.py. Colors are disabled automatically when
stdout is not a terminal (or NO_COLOR is set).
"""
import argparse
import itertools
import json
import logging
import os
import random
import re
import shlex
import shutil
import smtplib
import subprocess
import sys
import tempfile
import threading
import time
from email.message import EmailMessage
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import requests

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

from browser_use import browser_login, browser_task, instagram_upload, save_2fa, save_credentials
import chess_bot

__version__ = "2.1.0"

# OpenRouter free model
DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
DEFAULT_BASE = "https://openrouter.ai/api/v1"

SCRIPT_DIR = Path(__file__).resolve().parent

MEMORY_FILE = SCRIPT_DIR / "memory.txt"
CONV_LOG = SCRIPT_DIR / "conversations.jsonl"
MODEL_STATE_FILE = SCRIPT_DIR / "models_state.json"

MAX_CONV_LINES = 500
MAX_HISTORY = 40
MAX_TOOL_REPLY = 2500
MODEL_QUARANTINE_MIN = 15

BOT_MODE = os.environ.get("JORGE_BOT") == "1"

LAST_MODEL: str | None = None

LOG = logging.getLogger("jorge")

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64)"

# ---------------- pretty UI ----------------

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[38;5;114m"
CYAN = "\033[38;5;117m"
YELLOW = "\033[38;5;222m"
RED = "\033[38;5;203m"
MAGENTA = "\033[38;5;170m"
GRAY = "\033[38;5;245m"

_color_cache: bool | None = None


def _colors_on() -> bool:
    global _color_cache
    if _color_cache is None:
        _color_cache = (
            sys.stdout.isatty()
            and os.environ.get("NO_COLOR") is None
            and os.environ.get("TERM") != "dumb"
        )
    return _color_cache


def c(text: str, *codes: str) -> str:
    return "".join(codes) + text + RESET if _colors_on() else text


BANNER = c(
    """
  ┌─────────────────────────────────────────────┐
  │    J O R G E   Y O U R   A S S I S T A N T   │
  └─────────────────────────────────────────────┘
""",
    BOLD,
    CYAN,
)


def header(title: str, icon: str = "◆") -> None:
    print("\n" + c(f" {icon} {title} ", BOLD, MAGENTA) + c("─" * max(2, 46 - len(title)), GRAY))


def box(text: str, color: str = YELLOW) -> None:
    lines = text.split("\n")
    width = max(len(l) for l in lines)
    print(c("┌" + "─" * (width + 2) + "┐", GRAY))
    for l in lines:
        print(c("│ ", GRAY) + c(l.ljust(width), color) + c(" │", GRAY))
    print(c("└" + "─" * (width + 2) + "┘", GRAY))


def tokens_str(usage: dict | None, model: str | None = None) -> str:
    if not usage:
        return ""
    model_s = c(f" {model}", CYAN) if model else ""
    return c(
        f"  ⚡ {usage.get('prompt_tokens', '?')} in + {usage.get('completion_tokens', '?')} out tokens ·{model_s}",
        DIM,
    )


def choice_prompt() -> str:
    opts = [
        c("[" + c("a", BOLD, GREEN) + c("]pprove", GRAY)),
        c("[" + c("e", BOLD, GREEN) + c("]dit", GRAY)),
        c("[" + c("r", BOLD, GREEN) + c("]egenerate", GRAY)),
        c("[" + c("q", BOLD, GREEN) + c("]uit", GRAY)),
    ]
    return c("▶ ", CYAN) + "  ".join(opts) + c("  > ", CYAN)


def emit_progress(text: str) -> None:
    if BOT_MODE:
        print(json.dumps({"progress": text}), flush=True, file=sys.__stdout__)
    else:
        print(c(text, CYAN), flush=True)


def _safe_input(prompt: str, default: str = "") -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return default


def _readline_arrows(prompt: str, history: list[str]) -> str:
    """Terminal line editor with arrow keys (Up/Down history, Left/Right cursor). Falls back to input()."""
    import sys as _sys
    if not _sys.stdin.isatty():
        try:
            return input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return ""
    import select as _sel
    import termios as _termios
    import tty as _tty

    hist = list(history)
    hist_idx = len(hist)
    buf: list[str] = []
    cur = 0
    fd = _sys.stdin.fileno()
    out = _sys.stdout

    def _read_key() -> str:
        raw = _sys.stdin.buffer.read(1)
        if not raw:
            return ""
        if raw[0] == 0x1B:
            if _sel.select([_sys.stdin], [], [], 0.2)[0]:
                seq = _sys.stdin.buffer.read(2)
            else:
                return "\x1b"
            return "\x1b" + seq.decode("utf-8", "replace")
        if raw[0] < 0x80:
            return raw.decode("utf-8", "replace")
        seq = bytearray(raw)
        while True:
            try:
                return seq.decode("utf-8")
            except UnicodeDecodeError:
                seq += _sys.stdin.buffer.read(1)

    def _redraw() -> None:
        line = "".join(buf)
        out.write("\r\x1b[2K" + prompt + line)
        back = len(line) - cur
        if back > 0:
            out.write(f"\x1b[{back}D")
        out.flush()

    def _load(idx: int) -> None:
        nonlocal buf, cur, hist_idx
        hist_idx = idx
        buf = list(hist[hist_idx]) if 0 <= hist_idx < len(hist) else []
        cur = len(buf)
        _redraw()

    old = _termios.tcgetattr(fd)
    try:
        _tty.setraw(fd)
        out.write(prompt)
        out.flush()
        while True:
            ch = _read_key()
            if ch == "\x03":
                out.write("\r\n")
                out.flush()
                raise KeyboardInterrupt
            if ch == "\x04" and not buf:
                out.write("\r\n")
                out.flush()
                raise EOFError
            if ch in ("\r", "\n"):
                out.write("\r\n")
                out.flush()
                return "".join(buf)
            if ch == "\x7f":
                if cur > 0:
                    buf.pop(cur - 1)
                    cur -= 1
                    _redraw()
                continue
            if ch == "\x1b[A":
                if hist_idx > 0:
                    _load(hist_idx - 1)
                continue
            if ch == "\x1b[B":
                if hist_idx < len(hist):
                    _load(hist_idx + 1)
                continue
            if ch == "\x1b[C":
                if cur < len(buf):
                    cur += 1
                    _redraw()
                continue
            if ch == "\x1b[D":
                if cur > 0:
                    cur -= 1
                    _redraw()
                continue
            if ch == "\x1b[H":
                cur = 0
                _redraw()
                continue
            if ch == "\x1b[F":
                cur = len(buf)
                _redraw()
                continue
            if ch == "\x1b":
                continue
            if len(ch) == 1 and 32 <= ord(ch) < 127 or ord(ch) >= 160:
                buf.insert(cur, ch)
                cur += 1
                _redraw()
    finally:
        _termios.tcsetattr(fd, _termios.TCSADRAIN, old)


# ---------------- environment ----------------

def load_env(path: str | Path | None = None) -> dict[str, str]:
    path = Path(path) if path else SCRIPT_DIR / ".env"
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        if not key:
            continue
        env[key] = value
        if key not in os.environ:
            os.environ[key] = value
    return env


def check_env(env: dict[str, str]) -> None:
    missing = [k for k in ("AI_API_KEY", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_APP_PASSWORD") if not env.get(k)]
    if missing:
        print(c("✗ Missing in .env:", BOLD, RED), ", ".join(missing))
        sys.exit(1)


# ---------------- MEMORY ----------------

def load_memory() -> list[str]:
    if not MEMORY_FILE.exists():
        return []
    try:
        return [l.strip() for l in MEMORY_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        LOG.warning("could not read memory file")
        return []


MEMORY_MAX_LINES = 200
MISTAKES_FILE = SCRIPT_DIR / "mistakes.txt"
MISTAKES_MAX_LINES = 100


def load_mistakes() -> list[str]:
    if not MISTAKES_FILE.exists():
        return []
    try:
        return [l.strip() for l in MISTAKES_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return []


def save_mistake(text: str) -> None:
    text = text.strip()
    if not text:
        return
    lines = load_mistakes()
    if lines and lines[-1] == text:
        return
    lines.append(text)
    if len(lines) > MISTAKES_MAX_LINES:
        lines = lines[-MISTAKES_MAX_LINES:]
    try:
        MISTAKES_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        print(c("✗ could not write mistakes:", BOLD, RED), e)
        return
    print(c("✓ Learned.", BOLD, GREEN), c(f"({len(lines)} lessons total)", GRAY))


def save_memory_entry(text: str, replace_similar: bool = False) -> None:
    text = text.strip()
    if not text:
        return
    lines = load_memory()
    if lines and lines[-1] == text:
        return
    if replace_similar:
        key = text[:40]
        new_words = text.split()
        for i, existing in enumerate(lines):
            if existing[:40] == key:
                lines[i] = text
                break
            ewords = existing.split()
            if (
                len(ewords) >= 2
                and len(new_words) >= 2
                and ewords[:2] == new_words[:2]
                and (existing.startswith(text) or text.startswith(existing))
            ):
                lines[i] = text
                break
        else:
            lines.append(text)
    else:
        lines.append(text)
    if len(lines) > MEMORY_MAX_LINES:
        lines = lines[-MEMORY_MAX_LINES:]
    try:
        MEMORY_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        print(c("✗ could not write memory:", BOLD, RED), e)
        return
    print(c("✓ Remembered.", BOLD, GREEN), c(f"({len(lines)} notes total)", GRAY))


def forget_memory() -> None:
    if MEMORY_FILE.exists():
        MEMORY_FILE.unlink()
    print(c("✓ Memory cleared.", BOLD, GREEN))


PLAN_STATE_FILE = SCRIPT_DIR / "plan_state.json"
PLAN_GATED = ("shell", "write", "email", "email_draft", "sort", "organize", "delegate", "skill", "forget", "browser")
DISTILL_SCRIPT = SCRIPT_DIR / "distill.py"
DISTILL_LOCK = SCRIPT_DIR / "distill.lock"


def _spawn_distill() -> None:
    if not DISTILL_SCRIPT.exists():
        return
    if DISTILL_LOCK.exists():
        try:
            if time.time() - DISTILL_LOCK.stat().st_mtime < 600:
                return
        except OSError:
            return
    try:
        DISTILL_LOCK.write_text(str(os.getpid()), encoding="utf-8")
        subprocess.Popen(
            [sys.executable, str(DISTILL_SCRIPT)],
            cwd=str(SCRIPT_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def _load_plan_state() -> dict[str, Any]:
    try:
        return json.loads(PLAN_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"mode": False, "pending": [], "plans": []}


def _save_plan_state(state: dict[str, Any]) -> None:
    try:
        PLAN_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        LOG.warning("could not save plan state")


def _plan_step(tool: dict[str, Any]) -> str | None:
    a = tool.get("action")
    if a == "shell":
        return "run shell: " + str(_normalize_shell_cmd(tool.get("command", "")))[:80]
    if a == "write":
        return "write file: " + str(tool.get("path", "?"))
    if a == "email":
        return "send email to: " + str(tool.get("to", "?"))
    if a == "email_draft":
        return "draft and send email to: " + str(tool.get("to", "?"))
    if a in ("sort", "organize"):
        return "organize files in: " + str(tool.get("path", "?"))
    if a == "delegate":
        return "hand task to opencode (senior dev): " + str(tool.get("task", ""))[:60]
    if a == "skill":
        propose = tool.get("propose", [])
        names = ", ".join(s.get("name", "?") if isinstance(s, dict) else str(s) for s in propose[:5])
        return "build skills: " + names
    if a == "forget":
        return "clear stored memory notes"
    return None


def append_conversation(entry: dict[str, Any]) -> None:
    try:
        CONV_LOG.parent.mkdir(parents=True, exist_ok=True)
        with CONV_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        lines = CONV_LOG.read_text(encoding="utf-8").splitlines()
        if len(lines) > MAX_CONV_LINES:
            CONV_LOG.write_text("\n".join(lines[-MAX_CONV_LINES:]) + "\n", encoding="utf-8")
    except OSError:
        LOG.warning("could not append to conversation log")


def load_conversation(n: int) -> list[dict[str, str]]:
    try:
        if not CONV_LOG.exists():
            return []
        lines = [l for l in CONV_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
        out: list[dict[str, str]] = []
        for l in lines[-n * 4:]:
            try:
                entry = json.loads(l)
            except json.JSONDecodeError:
                continue
            if entry.get("role") in ("user", "assistant") and entry.get("content"):
                out.append({"role": entry["role"], "content": entry["content"][:2000]})
        return out[-n:]
    except (OSError, json.JSONDecodeError):
        return []


# ---------------- EMAIL ----------------

EMAIL_GARBAGE_MARKS = (
    "<system-reminder>", "operational mode", "analyze input", "step 1:", "step 2:", "step 3:",
    "here's my thinking", "let me think", "let me analyze", "i need to write", "1. analyze",
    "as an ai", "i cannot", "i'll write", "here's the email", "subject:", "thinking process",
)


def _email_looks_clean(body: str) -> bool:
    body = (body or "").strip()
    if not body or len(body) > 2500:
        return False
    low = body.lower()
    if any(mark in low for mark in EMAIL_GARBAGE_MARKS):
        return False
    if body.startswith(GARBLE_PREFIXES):
        return False
    if body.startswith("{") and '"' in body[:80]:
        return False
    return True


def draft_email(env: dict[str, str], to: str, subject: str, topic: str, tone: str = "professional",
                context: str | None = None) -> tuple[str, dict]:
    system = (
        "You are a helpful email-writing assistant. Write a clear, concise email "
        f"with a {tone} tone. Return ONLY the email body (text after the subject). "
        "No preamble, no signature, no 'Subject:' line. Never explain what you are doing, "
        "never number your thoughts, never mention system instructions — just the email text."
    )
    memory = load_memory()
    if memory:
        system += "\n\nNotes about the user and past topics (use them if relevant):\n- " + "\n- ".join(memory)
    user = f"Recipient: {to}\nSubject: {subject}\n"
    if context:
        user += f"Context:\n{context}\n"
    user += f"What the email should be about:\n{topic}"
    payload = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.7,
        "max_tokens": 500,
    }
    body = ""
    usage = {}
    for _attempt in range(3):
        body, usage, _model = api_call(env, payload, json_mode=False)
        body = (body or "").strip()
        if _email_looks_clean(body):
            break
        body = ""
        time.sleep(1.5)
    if not body:
        body = (f"Hi,\n\n{topic}\n\nBest regards.")[:2500]
    return body, usage


def edit_text(original: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as f:
        f.write(original)
        tmp = Path(f.name)
    editor = os.environ.get("EDITOR", "nano")
    try:
        subprocess.call([editor, str(tmp)])
        return tmp.read_text(encoding="utf-8").strip()
    finally:
        tmp.unlink(missing_ok=True)


def send_email(env: dict[str, str], to: str, subject: str, body: str, attachments: list[str] | None = None) -> None:
    if attachments:
        msg = MIMEMultipart("mixed")
        msg.attach(MIMEText(body, "plain"))
    else:
        msg = EmailMessage()
        msg.set_content(body)
    msg["Subject"] = subject
    msg["From"] = f"{env.get('SMTP_NAME', env['SMTP_USER'])} <{env['SMTP_USER']}>"
    msg["To"] = to
    for path in attachments or []:
        path = os.path.expanduser(path)
        fname = os.path.basename(path)
        with open(path, "rb") as fh:
            part = MIMEApplication(fh.read(), Name=fname)
        part["Content-Disposition"] = f'attachment; filename="{fname}"'
        msg.attach(part)
    with smtplib.SMTP(env["SMTP_HOST"], int(env["SMTP_PORT"])) as s:
        s.starttls()
        s.login(env["SMTP_USER"], env["SMTP_APP_PASSWORD"])
        s.send_message(msg)


def _email_approve_loop(env: dict[str, str], to: str, subject: str, topic: str,
                        tone: str = "professional", context: str | None = None) -> str | None:
    body, usage = draft_email(env, to, subject, topic, tone, context)
    print(c("  ── DRAFT ──", CYAN) + tokens_str(usage))
    box(body)
    if BOT_MODE:
        return body
    while True:
        action = _safe_input(choice_prompt(), default="q").lower()
        if action in ("q", "quit"):
            return None
        if action in ("r", "regenerate"):
            body, usage = draft_email(env, to, subject, topic, tone, context)
            print(c("  ── DRAFT ──", CYAN) + tokens_str(usage))
            box(body)
            continue
        if action in ("e", "edit"):
            body = edit_text(body)
            box(body)
            continue
        if action in ("a", "approve", ""):
            return body


def cmd_email(args: argparse.Namespace, env: dict[str, str]) -> None:
    check_env(env)
    print(BANNER)
    header(f"Drafting email with {env.get('AI_MODEL', DEFAULT_MODEL)}")
    print(c("  ✉ to: ", GRAY) + c(args.to, BOLD) + c("  ·  subject: ", GRAY) + c(args.subject, BOLD))
    body = _email_approve_loop(env, args.to, args.subject, args.topic, args.tone, args.context)
    if body is None:
        print(c("✗ cancelled", GRAY))
        sys.exit(0)
    print(c("  ✈ sending...", CYAN))
    try:
        send_email(env, args.to, args.subject, body)
    except Exception as e:
        print(c("  ✗ send failed:", BOLD, RED), e)
        sys.exit(1)
    print(c("  ✓ Sent to ", BOLD, GREEN) + c(args.to, BOLD) + "\n")


def email_flow(env: dict[str, str], to: str, subject: str, topic: str, tone: str = "professional", file: str | None = None) -> str:
    body = _email_approve_loop(env, to, subject, topic, tone)
    if body is None:
        return "cancelled"
    attachments = [file] if file else None
    if attachments:
        print(c("  📎 attaching: ", CYAN) + c(os.path.basename(attachments[0]), BOLD))
    print(c("  ✈ sending...", CYAN))
    send_email(env, to, subject, body, attachments)
    print(c("  ✓ Sent to ", BOLD, GREEN) + c(to, BOLD))
    return "sent"


# ---------------- FILE SORTING ----------------

CATEGORIES: dict[str, set[str]] = {
    "Images": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico", ".tiff", ".heic", ".raw"},
    "Videos": {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v"},
    "Audio": {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".wma", ".opus", ".mid", ".midi"},
    "Documents": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".md", ".rtf", ".odt", ".csv", ".epub"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".iso"},
    "Code": {".py", ".js", ".ts", ".tsx", ".jsx", ".gd", ".gdscript", ".tscn", ".sh", ".json", ".yaml", ".yml", ".html", ".css", ".c", ".cpp", ".h", ".java", ".rs", ".go", ".rb", ".php", ".sql"},
    "Installers": {".deb", ".rpm", ".apk", ".exe", ".msi", ".dmg", ".AppImage", ".flatpak", ".snap"},
}


def category_for(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return "Other"


def cmd_sort(args: argparse.Namespace, env: dict[str, str] | None = None) -> None:
    print(BANNER)
    folder = Path(args.path)
    if not folder.is_dir():
        print(c("✗ Not a directory:", BOLD, RED), folder)
        sys.exit(1)
    plan = {
        f: category_for(f.name)
        for f in sorted(folder.iterdir())
        if f.is_file() and not f.name.startswith(".")
    }
    if not plan:
        print(c("  no files to sort", GRAY))
        return
    header(f"Sorting {len(plan)} files in {folder}")
    for f, cat in plan.items():
        pad = 34 - len(f.name)
        print(c(f"  {f.name}", GRAY) + c(" " * max(pad, 1) + "→", CYAN) + c(f" {cat}/", BOLD, GREEN))
    if args.dry_run:
        print(c("  (dry run — nothing moved)", DIM))
        return
    answer = _safe_input(c("  proceed? ", CYAN) + c("[y/N] > ", GRAY), default="n").lower()
    if answer not in ("y", "yes"):
        print(c("  ✗ cancelled", GRAY))
        return
    for f, cat in plan.items():
        dest = folder / cat
        dest.mkdir(exist_ok=True)
        target = dest / f.name
        if target.exists():
            stem, ext = os.path.splitext(f.name)
            i = 1
            while target.exists():
                target = dest / f"{stem}_{i}{ext}"
                i += 1
        shutil.move(str(f), str(target))
        print(c(f"  ✓ {f.name} -> {cat}/", GREEN))
    print(c("  done.", BOLD, GREEN) + "\n")


# ---------------- WEB SEARCH ----------------

def _html_clean(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text).strip()


def _ddg_lite(query: str, limit: int) -> list[dict[str, str]]:
    r = requests.post(
        "https://lite.duckduckgo.com/lite/",
        data={"q": query},
        headers={"User-Agent": USER_AGENT},
        timeout=60,
    )
    r.raise_for_status()
    links = re.findall(r'<a rel="nofollow" href="([^"]+)" class=["\']result-link["\']>(.*?)</a>', r.text, re.S)
    snippets = re.findall(r'<td class=["\']result-snippet["\']>(.*?)</td>', r.text, re.S)
    out: list[dict[str, str]] = []
    for i, (url, title) in enumerate(links):
        snip = _html_clean(snippets[i]) if i < len(snippets) else ""
        out.append({"url": url, "title": _html_clean(title), "snippet": snip})
        if len(out) >= limit:
            break
    return out


def _ddg_html(query: str, limit: int) -> list[dict[str, str]]:
    r = requests.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers={"User-Agent": USER_AGENT},
        timeout=60,
    )
    r.raise_for_status()
    links = re.findall(r'<a rel="nofollow" class=["\']result__a["\'] href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S)
    snippets = re.findall(r'<a class=["\']result__snippet["\'] href="[^"]*"[^>]*>(.*?)</a>', r.text, re.S)
    out: list[dict[str, str]] = []
    for i, (url, title) in enumerate(links):
        snip = _html_clean(snippets[i]) if i < len(snippets) else ""
        out.append({"url": url, "title": _html_clean(title), "snippet": snip})
        if len(out) >= limit:
            break
    return out


def web_search(query: str, n: int = 8) -> list[dict[str, str]]:
    for attempt in range(2):
        for fetcher in (_ddg_lite, _ddg_html):
            try:
                results = fetcher(query, n)
                if results:
                    return results
            except (requests.RequestException, ValueError):
                LOG.debug("search backend %s failed", fetcher.__name__)
        if attempt == 0:
            time.sleep(1.5)
    return []


def cmd_web(args: argparse.Namespace, env: dict[str, str]) -> None:
    print(BANNER)
    print(c("  🔍 searching: ", GRAY) + c(args.query, BOLD) + c("  (DuckDuckGo)", DIM) + "\n")
    results = web_search(args.query)
    if not results:
        print(c("  ✗ no results found", GRAY))
        return
    for i, res in enumerate(results, 1):
        print(c(f"  {i:>2}. ", CYAN) + c(res["title"], BOLD))
        print(c("      " + res["url"], DIM))
        if res["snippet"]:
            print(c("      " + res["snippet"][:150], GRAY))
        print()
    if args.ask:
        if not env.get("AI_API_KEY"):
            print(c("✗ AI_API_KEY missing in .env — can't summarize.", BOLD, RED))
            return
        text = "\n".join(f"- {r['title']}: {r['snippet']}" for r in results)
        try:
            answer, usage, model = api_call(
                env,
                {
                    "messages": [
                        {"role": "system", "content": "Answer the user's question using ONLY the search results. Be concise."},
                        {"role": "user", "content": f"Question: {args.query}\n\nSearch results:\n{text}"},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 400,
                },
                json_mode=False,
            )
        except RuntimeError as e:
            print(c("  ✗ AI summary failed: ", RED) + str(e))
            print()
            return
        print(c("  ── AI ANSWER ──", CYAN) + tokens_str(usage, model))
        box(answer, GREEN)
    print()


def cmd_music(args: argparse.Namespace, env: dict[str, str]) -> None:
    print(BANNER)
    print(c("  🎵 searching: ", GRAY) + c(args.query, BOLD) + c("  (Spotify)", DIM) + "\n")
    results = web_search(f"site:open.spotify.com {args.query}")
    spotify = next((r["url"] for r in results if "open.spotify.com" in r["url"]), None)
    if not spotify:
        print(c("  ✗ no Spotify link found", GRAY))
        print()
        return
    print(c("  Spotify link:", CYAN))
    box(spotify, GREEN)
    print()


# ---------------- MEMORY COMMANDS ----------------

def cmd_browser(args: argparse.Namespace, env: dict[str, str]) -> None:
    task = args.task
    headed = bool(re.search(r"\bheaded\s*$", task, re.I))
    if headed:
        task = re.sub(r"\s*headed\s*$", "", task, flags=re.I).strip()
    print(BANNER)
    print(c("  🌐 browser task: ", GRAY) + c(task, BOLD)
          + (c("  (visible window)", DIM) if headed else c("  (headless Chrome)", DIM)) + "\n")
    result = browser_task(task, args.url or "", decide=_browser_decide, ask=ask_bot,
                          progress=emit_progress, headless=not headed)
    print()
    box(result, GREEN if result and not result.startswith(("⚠", "🔐", "I got")) else YELLOW)
    print()


def cmd_browser_login(args: argparse.Namespace, env: dict[str, str]) -> None:
    if BOT_MODE:
        print(c("  browser-login needs your keyboard — run it in the terminal: ", GRAY)
              + c(f"jorge browser-login {args.site}", BOLD))
        return
    print(BANNER)
    print(c("  🔐 visible login for ", GRAY) + c(args.site, BOLD) + "\n")
    result = browser_login(args.site, progress=emit_progress)
    print()
    box(result, GREEN if result.startswith("✅") else YELLOW)
    print()


def _find_media(arg: str) -> str:
    """Resolve a video file: full path, filename, or auto-pick the newest video if empty."""
    from pathlib import Path as _Path
    if not arg:
        arg = "*.mp4"
    direct = _Path(arg).expanduser()
    if direct.exists() and direct.is_file():
        return str(direct)
    dirs = [p.expanduser() for p in (_Path("~/Videos"), _Path("~/Downloads"), _Path("~/Desktop")) if p.expanduser().is_dir()]
    exact = [p for d in dirs for p in d.rglob(arg) if p.is_file()]
    if not exact:
        low = arg.lower().lstrip("*")
        exact = [p for d in dirs for p in d.rglob("*") if p.is_file() and p.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm") and low in p.name.lower()]
    if not exact:
        return ""
    return str(max(exact, key=lambda p: p.stat().st_mtime))


def cmd_instagram_upload(args: argparse.Namespace, env: dict[str, str]) -> None:
    if BOT_MODE:
        print(c("  instagram-upload needs the terminal — run: ", GRAY)
              + c(f"jorge instagram-upload \"{args.file}\"", BOLD))
        return
    pieces = list(args.file)
    headed = bool(re.search(r"\bheaded$", pieces[-1], re.I)) if pieces else False
    if headed:
        pieces.pop()
    path = _find_media(" ".join(pieces).strip())
    if not path:
        print(BANNER)
        print(c("  📤 instagram upload: ", GRAY) + c("no video found for", RED)
              + c(" ".join(pieces).strip() or "<none>", BOLD) + "\n")
        print(c("  recent videos:", GRAY))
        from pathlib import Path as _Path
        for d in (_Path("~/Videos"), _Path("~/Downloads"), _Path("~/Desktop")):
            d = d.expanduser()
            if not d.is_dir():
                continue
            for p in sorted(d.rglob("*"), key=lambda p: p.stat().st_mtime if p.is_file() else 0, reverse=True):
                if p.is_file() and p.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm"):
                    print(c(f"   - {p}", DIM))
                    break
        print()
        return
    print(BANNER)
    print(c("  📤 instagram upload: ", GRAY) + c(os.path.basename(path), BOLD)
          + c("  (visible window — needed for the share to register)", DIM) + "\n")
    result = instagram_upload(path, args.caption or "", progress=emit_progress, headless=False)
    print()
    box(result, GREEN if result.startswith("✅") else YELLOW)
    print()


def cmd_remember(args: argparse.Namespace, env: dict[str, str]) -> None:
    print(BANNER)
    header("Storing note")
    text = args.text
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not text:
        print(c("  (nothing to remember — pass text or pipe it in)", GRAY))
        return
    save_memory_entry(text)
    print()


def cmd_memory(args: argparse.Namespace, env: dict[str, str]) -> None:
    print(BANNER)
    header("Memory notes")
    notes = load_memory()
    if not notes:
        print(c("  (nothing stored yet — use: jorge remember \"...\")", GRAY))
        return
    for i, n in enumerate(notes, 1):
        print(c(f"  {i:>2}. ", CYAN) + c(n, GRAY))
    print()


def cmd_forget(args: argparse.Namespace, env: dict[str, str]) -> None:
    print(BANNER)
    forget_memory()
    print()


# ---------------- FILE ORGANIZATION (patterns) ----------------

PATTERN_RULES: dict[str, str] = {
    r"^IMG[_\- ]?\d+": "Phone_Photos",
    r"^Screenshot[_\- ]?\d*": "Screenshots",
    r"^WhatsApp(?: Image| Video)?[_\- ]?\d*": "WhatsApp_Media",
    r"^resume|^cv\b|^cover[_\- ]?letter": "Job_Search",
    r"invoice|receipt|bill|statement": "Finances",
    r"tax|_202\d|_20\d\d": "Taxes",
    r"report|meeting[_\- ]?notes|notes": "Work_Docs",
    r"backup|\.bak$|_old|copy\b": "Backups",
    r"^flac_": "Flac_Album",
}


def pattern_category_for(filename: str) -> str:
    name = Path(filename).name
    for rule, cat in PATTERN_RULES.items():
        if re.search(rule, name, re.I):
            return cat
    return category_for(filename)


def cmd_organize(args: argparse.Namespace, env: dict[str, str] | None = None) -> None:
    print(BANNER)
    folder = Path(args.path)
    if not folder.is_dir():
        print(c("✗ Not a directory:", BOLD, RED), folder)
        sys.exit(1)
    plan = {
        f: pattern_category_for(f.name)
        for f in sorted(folder.iterdir())
        if f.is_file() and not f.name.startswith(".")
    }
    if not plan:
        print(c("  no files to organize", GRAY))
        return
    header(f"Organizing {len(plan)} files in {folder} (type + naming patterns)")
    for f, cat in plan.items():
        pad = 36 - len(f.name)
        print(c(f"  {f.name}", GRAY) + c(" " * max(pad, 1) + "→", CYAN) + c(f" {cat}/", BOLD, GREEN))
    if args.dry_run:
        print(c("  (dry run — nothing moved)", DIM))
        return
    answer = _safe_input(c("  proceed? ", CYAN) + c("[y/N] > ", GRAY), default="n").lower()
    if answer not in ("y", "yes"):
        print(c("  ✗ cancelled", GRAY))
        return
    for f, cat in plan.items():
        dest = folder / cat
        dest.mkdir(exist_ok=True)
        target = dest / f.name
        if target.exists():
            stem, ext = os.path.splitext(f.name)
            i = 1
            while target.exists():
                target = dest / f"{stem}_{i}{ext}"
                i += 1
        shutil.move(str(f), str(target))
        print(c(f"  ✓ {f.name} -> {cat}/", GREEN))
    print(c("  done.", BOLD, GREEN) + "\n")


# ---------------- WEB RESEARCH (citations) ----------------

def web_research(query: str, n: int = 8) -> list[dict[str, str]]:
    return web_search(query, n)


def deep_research(query: str, min_docs: int = 5) -> list[dict[str, str]]:
    queries = [query]
    env = load_env()
    if env.get("AI_API_KEY"):
        try:
            raw, _u, _m = chat_call(
                env,
                [
                    {"role": "system", "content": "Return a JSON array of 3-4 short web search queries (strings) that together thoroughly cover the user's topic from different angles. Only the array, no other text."},
                    {"role": "user", "content": query},
                ],
                max_tokens=120,
            )
            extra = json.loads(raw.strip())
            if isinstance(extra, list):
                queries += [str(q).strip()[:120] for q in extra if isinstance(q, str) and q.strip()][:3]
        except Exception:
            pass

    seen: dict[str, dict[str, str]] = {}

    def collect(q: str) -> None:
        for res in web_search(q, 6):
            url = re.sub(r"#.*$", "", res.get("url", "")).rstrip("/")
            if url and url not in seen:
                seen[url] = {"title": res.get("title", ""), "url": res.get("url", ""), "snippet": res.get("snippet", "")}

    for q in queries:
        if len(seen) >= min_docs:
            break
        collect(q)
    for suffix in ("sources", "guide", "explain", "examples"):
        if len(seen) >= min_docs:
            break
        collect(f"{query} {suffix}")
    if PRICE_RE.search(query):
        collect(f"{query} price India INR")
    return list(seen.values())[: min_docs + 3]


RESEARCH_EXEMPT = re.compile(
    r"\b(?:jorge|yourself|your name|your code|what can you|who are you|"
    r"remember|memory|forget|plan mode|abort|stop|cancel)\b",
    re.I,
)

PRICE_RE = re.compile(r"\b(?:price|prices|pricing|cost|costs|how much|worth|rupees?|\brs\.?|bucks|₹|\$)\b", re.I)


GARBLE_PREFIXES = (
    "here's a thinking", "let me think", "i need to think", "1. **analyze",
    "the user is asking", "i need to provide", "the instruction says", "i should",
    "based on the information given", "let me provide", "i don't have exact",
)


def _clean_answer(text: str) -> bool:
    return bool(text) and not text.lower().startswith(GARBLE_PREFIXES)


def _source_fallback(docs: list[dict[str, str]], header: str) -> str:
    lines = []
    for i, d in enumerate(docs[:5], 1):
        snippet = (d.get("snippet") or "").strip()[:200]
        lines.append(f"{i}. {d.get('title', '?')}" + (f" — {snippet}" if snippet else ""))
    return header + "\n" + "\n".join(lines)


def _price_note(question: str) -> str:
    if PRICE_RE.search(question):
        return (
            " The question asks about PRICE/COST: your answer MUST state the current price "
            "or price range as the FIRST thing, with the date it refers to (and in INR/₹ if India-relevant)."
        )
    return ""


def _reply_is_garbage(reply: str) -> bool:
    r = reply.strip()
    first = r.splitlines()[0].lower() if r else ""
    if first.startswith(("no results found", "search returned nothing")):
        return True
    if r.startswith("{") and '"action"' in r[:120]:
        return True
    return len(r) < 40 and r.lower().startswith(("i searched", "i looked", "done", "ok", "no results"))


def _wants_research(line: str) -> bool:
    low = re.sub(r"^(?:ok|okay|sure|hmm|um|so|yo|hey)\s*[,!.]*\s*", "", line.strip().lower())
    if len(low) < 6:
        return False
    if RESEARCH_EXEMPT.search(low):
        return False
    if re.match(r"^(?:hi|hello|hey|yo|thanks|thank you|ok|okay|sure|yes|no|bye)\s*[.!]?$", low):
        return False
    if low.rstrip().endswith("?"):
        return True
    return bool(
        re.match(
            r"^(?:whats?|hows?|whys?|whos?|whens?|wheres?|whichs?|is|are|do|does|can|could|"
            r"should|would|will|tell me|explain|plan|research|look up|find|search|"
            r"best|top|compare|recommend|give me)\b",
            low,
        )
    )


def format_research_cited(results: list[dict[str, str]]) -> str:
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['title']}")
        lines.append(f"    Source: {r['url']}")
        if r.get("snippet"):
            lines.append(f"    {r['snippet'][:180]}")
    return "\n".join(lines)


def cmd_research(args: argparse.Namespace, env: dict[str, str]) -> None:
    print(BANNER)
    print(c("  🔍 researching: ", GRAY) + c(args.query, BOLD) + c("  (DuckDuckGo, cited)", DIM) + "\n")
    results = web_research(args.query, args.limit)
    if not results:
        print(c("  ✗ no results found", GRAY))
        return
    print(format_research_cited(results))
    if args.ask:
        if not env.get("AI_API_KEY"):
            print(c("✗ AI_API_KEY missing in .env — can't summarize.", BOLD, RED))
            return
        text = "\n".join(f"[{i}] {r['title']}: {r['url']} — {r['snippet']}" for i, r in enumerate(results, 1))
        try:
            answer, usage, model = api_call(
                env,
                {
                    "messages": [
                        {"role": "system", "content": "Summarize the findings for the user's question. Cite sources by their [n] number. Be concise but complete."},
                        {"role": "user", "content": f"Question: {args.query}\n\nSearch results:\n{text}"},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 500,
                },
                json_mode=False,
            )
        except RuntimeError as e:
            print(c("  ✗ AI summary failed: ", RED) + str(e))
            print()
            return
        print(c("  ── AI SUMMARY (with citations) ──", CYAN) + tokens_str(usage, model))
        box(answer, GREEN)
    print()


# ---------------- EMAIL DRAFTING (tone + context) ----------------

EMAIL_TONES = {"professional", "friendly", "casual", "formal", "warm", "direct", "urgent"}

EMAIL_TONE_GUIDE = {
    "professional": "Clear, courteous, well-structured, standard business tone. Use complete sentences and avoid slang.",
    "friendly": "Approachable and personable while staying professional. Warm greetings, light touch.",
    "casual": "Relaxed, conversational, like texting a close colleague. Can use contractions and informality.",
    "formal": "Highly polished and respectful. Formal salutations, no contractions, deferential language.",
    "warm": "Kind and encouraging, relationship-focused, genuinely interested in the recipient.",
    "direct": "Brief and to-the-point. Few pleasantries, straight to the ask, still polite.",
    "urgent": "Clear about time-sensitivity. Flags the deadline and why it matters, while staying professional.",
}


def _recent_context() -> str:
    entries = []
    try:
        lines = CONV_LOG.read_text(encoding="utf-8").splitlines()[-12:]
    except OSError:
        return ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = entry.get("role", "")
        content = (entry.get("content") or "")[:400]
        if role in ("user", "assistant") and content:
            entries.append(f"{role}: {content}")
    return "\n".join(entries)


def cmd_email_draft(args: argparse.Namespace, env: dict[str, str]) -> None:
    print(BANNER)
    if not env.get("AI_API_KEY"):
        print(c("✗ AI_API_KEY missing in .env.", BOLD, RED))
        sys.exit(1)
    tone = (args.tone or "professional").lower()
    if tone not in EMAIL_TONES:
        print(c(f"✗ unknown tone '{tone}'. choose: {', '.join(sorted(EMAIL_TONES))}", BOLD, RED))
        return
    header(f"Drafting {tone} email to {args.to}")
    print(c("  ✉ subject: ", GRAY) + c(args.subject or "(AI-generated)", BOLD))
    guide = EMAIL_TONE_GUIDE.get(tone, EMAIL_TONE_GUIDE["professional"])
    context = args.context or ""
    if args.recall:
        context = (context + "\n" if context else "") + "Relevant recent conversation:\n" + _recent_context()
    try:
        body, usage, model = api_call(
            env,
            {
                "messages": [
                    {"role": "system", "content": "You are a skilled email writer. Write only the email body (no subject line, no salutation wrapper unless requested)."},
                    {"role": "user", "content": f"Recipient: {args.to}\nTone: {tone} ({guide})\nSubject: {args.subject or '(create one suggestion inside the body as a first line)'}\n"
                                              f"Content to convey: {args.topic}\n\nAdditional context to weave in naturally: {context or '(none)'}"},
                ],
                "temperature": 0.6,
                "max_tokens": 600,
            },
            json_mode=False,
        )
    except RuntimeError as e:
        print(c("  ✗ AI draft failed: ", RED) + str(e))
        return
    print(c("  ── DRAFT ──", CYAN) + tokens_str(usage, model))
    box(body, GREEN)
    if not args.no_send:
        if confirm_action(f"send this {tone} email to {args.to}"):
            send_email(env, args.to, args.subject or "(no subject)", body)
            print(c("  ✓ Sent to ", BOLD, GREEN) + c(args.to, BOLD) + "\n")
        else:
            print(c("  draft saved locally only — use: jorge email to send later", GRAY))
            print()


# ---------------- SYSTEM MONITORING ----------------

def fmt_bytes(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}T"


def collect_system_stats() -> dict[str, Any]:
    stats: dict[str, Any] = {}
    if psutil is None:
        return stats
    vm = psutil.virtual_memory()
    stats["cpu_percent"] = psutil.cpu_percent(interval=0.2)
    stats["mem_percent"] = vm.percent
    stats["mem_used"] = vm.used
    stats["mem_total"] = vm.total
    du = shutil.disk_usage(Path.home())
    stats["disk_percent"] = round(du.used / du.total * 100, 1)
    stats["disk_used"] = du.used
    stats["disk_total"] = du.total
    stats["loadavg"] = [round(x, 2) for x in os.getloadavg()]
    stats["procs"] = len(psutil.pids())
    return stats


def top_processes(n: int = 5) -> list[dict[str, Any]]:
    if psutil is None:
        return []
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            procs.append(p.info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(key=lambda x: (x.get("cpu_percent") or 0) + (x.get("memory_percent") or 0), reverse=True)
    return procs[:n]


def _fmt_stats(stats: dict[str, Any]) -> str:
    lines = [
        f"CPU: {stats.get('cpu_percent', '?')}%",
        f"Memory: {fmt_bytes(stats.get('mem_used', 0))} / {fmt_bytes(stats.get('mem_total', 0))} ({stats.get('mem_percent', '?')}%)",
        f"Disk (~): {fmt_bytes(stats.get('disk_used', 0))} / {fmt_bytes(stats.get('disk_total', 0))} ({stats.get('disk_percent', '?')}%)",
        f"Load average: {stats.get('loadavg', '?')}",
        f"Processes: {stats.get('procs', '?')}",
    ]
    return "\n".join(lines)


def check_alerts(thresholds: dict[str, float]) -> list[str]:
    alerts = []
    stats = collect_system_stats()
    if not stats:
        return ["(system monitoring unavailable — psutil not installed)"]
    if stats.get("cpu_percent", 0) >= thresholds.get("cpu", 90):
        alerts.append(f"⚠ CPU at {stats['cpu_percent']}% (threshold {thresholds['cpu']}%)")
    if stats.get("mem_percent", 0) >= thresholds.get("mem", 85):
        alerts.append(f"⚠ Memory at {stats['mem_percent']}% (threshold {thresholds['mem']}%)")
    if stats.get("disk_percent", 0) >= thresholds.get("disk", 90):
        alerts.append(f"⚠ Disk at {stats['disk_percent']}% (threshold {thresholds['disk']}%)")
    if not alerts:
        alerts.append("✓ All systems within normal thresholds.")
    return alerts


def cmd_monitor(args: argparse.Namespace, env: dict[str, str] | None = None) -> None:
    print(BANNER)
    if psutil is None:
        print(c("✗ psutil not installed — run: pip install psutil", BOLD, RED))
        return
    header("System monitoring")
    stats = collect_system_stats()
    print(c("  " + _fmt_stats(stats).replace("\n", "\n  "), GRAY) + "\n")
    header("Top processes (cpu + memory)")
    for p in top_processes(args.top):
        print(c(f"  {p.get('name', '?')}", BOLD) + c(f"  pid={p.get('pid')}", DIM) +
              c(f"  cpu={round(p.get('cpu_percent') or 0, 1)}%  mem={round(p.get('memory_percent') or 0, 1)}%", GRAY))
    print()
    thresholds = {"cpu": args.cpu, "mem": args.mem, "disk": args.disk}
    print(c("  thresholds: ", GRAY) + c(f"cpu>{args.cpu}%  mem>{args.mem}%  disk>{args.disk}%", DIM))
    for a in check_alerts(thresholds):
        color = RED if a.startswith("⚠") else GREEN
        print(c("  " + a, color))
    print()


# ---------------- TASK DELEGATION (breakdown) ----------------

def break_down_task(task: str, max_steps: int = 5) -> list[str]:
    steps = []
    lines = [l.strip(" -•\t") for l in task.splitlines() if l.strip()]
    if not lines:
        return []
    if len(lines) >= 2:
        for l in lines[:max_steps]:
            if not l.lower().startswith(("step", "then", "next", "finally", "first", "second", "third")):
                steps.append(l)
            else:
                steps.append(l)
        return steps[:max_steps]
    words = re.findall(r"[a-zA-Z]+", task)
    keywords = [w for w in words if w.lower() in (
        "install", "installing", "setup", "set", "create", "creating", "build", "building", "write", "writing",
        "test", "tests", "testing", "refactor", "fix", "fixing", "configure", "configuring", "update",
        "deploy", "deploying", "debug", "review", "document", "rename", "move", "add", "remove", "send",
    )]
    if keywords:
        positions = []
        for k in keywords:
            m = re.search(r"\b" + re.escape(k) + r"\b", task, re.I)
            if m:
                positions.append((m.start(), k))
        positions.sort()
        seen: set[str] = set()
        unique: list[tuple[int, str]] = []
        for pos, k in positions:
            if k.lower() not in seen:
                seen.add(k.lower())
                unique.append((pos, k))
        for i, (pos, k) in enumerate(unique[:max_steps]):
            start = pos
            if i + 1 < len(unique):
                end = unique[i + 1][0]
            else:
                end = len(task)
            step = task[start:end].strip().rstrip(",;").strip()[:160]
            steps.append(step or k)
    if not steps:
        steps = [task[:160]] * 1
    return steps[:max_steps]


def cmd_delegate_breakdown(args: argparse.Namespace, env: dict[str, str]) -> None:
    print(BANNER)
    task = args.task
    header("Task breakdown")
    steps = break_down_task(task, args.steps)
    for i, s in enumerate(steps, 1):
        print(c(f"  {i}. ", CYAN) + c(s, BOLD))
    print()
    if not args.dry_run and env.get("AI_API_KEY"):
        if confirm_action("hand the full task to opencode (senior dev)") or BOT_MODE:
            emit_progress("⚡ Delegating full task to my senior dev (opencode)...")
            out = run_delegate(task, args.dir or "")
            print(c("  ── RESULT ──", CYAN))
            print(c(_keep_tail_summary(summarize_delegate(out)), GRAY))
    print()


# ---------------- CHAT / MODEL ----------------

CHAT_SYSTEM = """You are jorge, a friendly personal assistant in a terminal with FULL access to the user's computer.
Facts: the user's HOME folder is {HOME}, username is {USER}, OS is Linux.
The user talks to you in plain language.
You must reply with ONLY one JSON object, no markdown fences, no extra text, no reasoning, no comments. Never output anything outside the JSON object. Pick exactly one action:

{"action": "chat", "reply": "your answer"}
{"action": "web", "query": "search query"}
{"action": "email", "to": "recipient@example.com", "subject": "...", "topic": "what the email should say", "file": "/absolute/path/to/file.zip"}  # file is OPTIONAL: to attach a file, FIRST locate it with a shell command, then pass its real absolute path in "file". Never invent a path.
{"action": "sort", "path": "/folder/path"}
{"action": "remember", "text": "note to store"}
{"action": "memory"}
{"action": "forget"}
{"action": "shell", "command": "terminal command to run"}
{"action": "read", "path": "/path/to/file"}
{"action": "write", "path": "/path/to/file", "content": "file contents"}
{"action": "list", "path": "/folder"}
{"action": "self", "task": "status|view|edit|debug", "edits": [{"old": "...", "new": "..."}], "replace": "optional full new file source"}
{"action": "delegate", "task": "the full task to hand to opencode", "dir": "optional working folder"}
{"action": "skill", "propose": [{"name": "skill-name", "purpose": "one line"}]}
{"action": "organize", "path": "/folder/path"}
{"action": "research", "query": "complex question", "n": 8}
{"action": "brainstorm", "query": "topic to brainstorm"}
{"action": "chess", "query": "FEN, move list (1. e4 e5 ...), 'start', or 'play me'"}
{"action": "chess_vs", "query": "elo number for jorge (e.g. 1500)"}
{"action": "chess_move", "query": "your move in SAN (e.g. e4, Nf3, O-O) or 'resign'"}
{"action": "email_draft", "to": "recipient@example.com", "subject": "...", "topic": "what it should say", "tone": "professional|friendly|casual|formal|warm|direct|urgent", "context": "optional extra context", "recall": true}
{"action": "monitor", "top": 5, "cpu": 90, "mem": 85, "disk": 90}
{"action": "delegate_breakdown", "task": "the complex task", "dir": "optional folder", "steps": 5}
{"action": "browser", "task": "what to do in the real browser", "url": "optional start URL"}
{"action": "browser_creds", "site": "discord.com", "email": "you@x.com", "password": "hunter2"}
{"action": "browser_2fa", "site": "discord.com", "code": "123456"}

Rules:
- If the user's message starts with an email address (e.g. "email someone@x.com ask about X") -> action email with that exact address, subject and topic derived from the rest. NEVER use action browser or chat for sending emails.
- NEVER claim an email was sent unless you actually ran the email action. If you didn't run it, say you couldn't send it.
- If the user asks to email/send a message -> action email. Extract recipient, subject, and the content/topic. To find the recipient's address: FIRST check the Memory notes for a "CONTACT: <Name>'s email is <addr>" entry, THEN recent conversation history, THEN ask the user. Never invent or guess an email address.
- If the user asks to search/check something online -> action web. If the user wants a deeper, cited research summary of a complex question -> action research.
- RESEARCH FIRST: if the user asks a question or asks you to plan something, search the web first (action web or action research, multiple targeted queries) and read at least 5 sources before answering. Base your answer on those sources and cite a couple of them. Skip research only for questions about the user/jorge itself, memory notes, or small talk.
- If the user asks to organize/sort files -> action sort (basic by type) or action organize (by type AND naming patterns like IMG_, Screenshot, invoice, resume, etc).
- If the user asks to draft an email with a specific tone (professional/friendly/casual/formal/warm/direct/urgent) or wants a draft they can review before sending -> action email_draft.
- If the user asks to check system resources, CPU/memory/disk usage, running processes, or wants alerts on thresholds -> action monitor.
- If the user asks to plan/break down a complex multi-step task into sub-tasks (then delegate them) -> action delegate_breakdown.
- If the user asks about chess (analyze a position, best move, who's winning, what should I play) or wants to play a game -> action chess. Positions can be a FEN, a move list (1. e4 e5 ...), or 'start'. If they say 'play me', analyze the current position and play the best move from the result.
- If the user wants to PLAY chess against jorge (vs, play a game, challenge) -> action chess_vs with an elo number (jorge's strength, 500-3190). When the user then sends a chess move (e4, Nf3, O-O, 'resign') -> action chess_move with that move. Report the board from the tool output.
- If the user asks to do something in a real web browser (login to sites, create a Discord server/channel, fill forms, download files, take screenshots) -> action browser with task = exactly what the user asked. It may also be needed when the info is only reachable behind a login. It needs plan-mode approval like shell.
- BROWSER LOGIN FLOW: when a browser action reports it needs credentials or a 2FA code for a site, repeat that question to the user as your reply. When the user replies with the credentials (email/password) or a 2FA code, FIRST save them with action browser_creds (site, email, password) or browser_2fa (site, code), THEN retry the original browser task. Never guess or invent credentials.
- ACCOUNT LOGINS ARE ALLOWED: the user owns this machine and these accounts. When the user asks you to log into THEIR OWN account (Discord, Spotify, email, etc.) or do things inside it (create servers, join servers, manage things), that is normal, permitted work — do it with action browser. Do NOT refuse or lecture about safety; do NOT use browser_creds/browser_2fa to guess credentials. Never log in to accounts the user did not ask about, and never share the stored credentials with anyone.
- If the user says remember/note that -> action remember.
- If the user asks to run a command, install something, check system info, create/edit code files, inspect files or folders -> use shell/read/write/list. Break big tasks into small steps (one action per reply).
- DELEGATE RULE: You are the DISPATCHER for coding/building work. If the task involves writing code, building a feature/project, fixing bugs, refactoring, scripting, game development, or anything a senior developer would do -> action delegate with the user's request as "task". Never attempt complex coding yourself. Small terminal commands (ls, checking info, simple one-liners) are fine as shell.
- MULTI-STEP FILE RULE: If a task needs several steps (compare two folders, bulk delete/move files matching across directories, find+edit many files), use action delegate — it handles multi-step work reliably. Do NOT grind through it with repeated shell calls.
- NEVER delegate or touch anything related to the Godot game project (godot, my-game-my-legacy-2, boss, tscn, win_screen). That is off-limits — the boss's rules.
- If the user asks you to build yourself new skills / improve yourself / make yourself smarter -> action skill with "propose" = a SPECIFIC list of 3-5 concrete skills for YOURSELF (name + one-line purpose), based on what the user uses you for. Never reply vaguely about "skills". Never propose skills about the Godot game project.
- For searching for files, ALWAYS add -maxdepth 5 and 2>/dev/null (e.g. find {HOME} -maxdepth 5 -iname "*.exe" 2>/dev/null). Full scans of the home folder time out — never run find without -maxdepth.
- After a shell/read/write/list action, you will receive the tool output and can continue.
- If the user replies with just yes/no/ok/cancel, it is answering YOUR most recent question — look at your previous reply to know what it refers to. Restate what you are doing (e.g. "Deleting the 3 .tmp files") before doing destructive things like deleting.
- If you listed files and asked which to delete and the user picks ("all of them", numbers, names), delete EXACTLY those from the list — the list is provided in the user's message marked "(The files you just listed...)". Act on it immediately with ONE shell command (e.g. rm -f ...). Never ask again which files — the answer is already given.
 - Whenever you show output (list/read/shell/web/sort), ALWAYS end with a short 1-2 sentence acknowledgment of what you found and ask what the user wants to do next (unless you already answered the question).
- SELF-AWARENESS: You are jorge, a Python program. Your own source code is {SCRIPT_DIR}/assistant.py, and your own files are assistant.py, .env, memory.txt, conversations.jsonl, requirements.txt, .env.example. Use action self to inspect (view/status), modify (edit), or verify (debug) YOUR OWN code. To change a specific string: FIRST run self task=view with find="<a few words of the current string>" (this returns only the surrounding lines — never dump the whole file), THEN self task=edit with 'edits' containing the EXACT lines you just saw (never invent text). Use 'replace' only for a full rewrite. After editing, ALWAYS run self task=debug to confirm the script still compiles. Never edit other programs' source with 'self'.
- Anything else -> action chat with a helpful, short reply.
"""


FALLBACK_MODELS = ["deepseek-v4-flash-free", "nemotron-3-ultra-free", "nemotron-3.5-lightning-free", "laguna-s-2.1-free", "mimo-v2.5-free", "hy3-free", "big-pickle"]

MODEL_WEIGHTS = {
    "laguna-s-2.1-free": 5,
    "deepseek-v4-flash-free": 5,
    "nemotron-3-ultra-free": 4,
    "nemotron-3.5-lightning-free": 2,
    "hy3-free": 1,
    "mimo-v2.5-free": 1,
    "big-pickle": 3,
}


class Spinner:
    def __init__(self, label: str = "thinking") -> None:
        self.label = label
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def start(self) -> None:
        self._t = threading.Thread(target=self._spin, daemon=True)
        self._t.start()

    def _spin(self) -> None:
        for ch in itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
            if self._stop.is_set():
                break
            sys.stdout.write(f"\r\033[36m{ch}\033[0m {self.label}...")
            sys.stdout.flush()
            time.sleep(0.08)

    def stop(self) -> None:
        self._stop.set()
        if self._t:
            self._t.join(timeout=0.5)
        sys.stdout.write("\r" + " " * 40 + "\r")
        sys.stdout.flush()


def _load_model_state() -> dict[str, Any]:
    try:
        return json.loads(MODEL_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"skip": {}, "last_index": 0}


def _save_model_state(state: dict[str, Any]) -> None:
    try:
        MODEL_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        LOG.warning("could not save model state")


def _pick_model(pool: list[str]) -> str:
    weights = [MODEL_WEIGHTS.get(m, 1) for m in pool]
    return random.choices(pool, weights=weights, k=1)[0]


def _looks_parseable(text: str) -> bool:
    """True if the model output can be turned into the expected JSON (or is a <tool_call>)."""
    if "<tool_call" in text:
        return True
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        json.loads(t)
        return True
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            json.loads(m.group(0))
            return True
        except json.JSONDecodeError:
            pass
    return False


def api_call(env: dict[str, str], payload: dict[str, Any], json_mode: bool = True) -> tuple[str, dict, str]:
    base = env.get("AI_BASE_URL", DEFAULT_BASE).rstrip("/")
    url = base + "/chat/completions"
    headers = {"Authorization": f"Bearer {env['AI_API_KEY']}", "Content-Type": "application/json"}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    spinner = Spinner("thinking")
    if not BOT_MODE and _colors_on():
        spinner.start()
    try:
        state = _load_model_state()
        now = time.time()
        skip = {m: t for m, t in state.get("skip", {}).items() if now - t < MODEL_QUARANTINE_MIN * 60}
        pool = []
        for m in [env.get("AI_MODEL", DEFAULT_MODEL)] + FALLBACK_MODELS:
            if m not in pool and m not in skip:
                pool.append(m)
        if not pool:
            pool = [env.get("AI_MODEL", DEFAULT_MODEL)]
        chosen = _pick_model(pool)
        models = [chosen] + [m for m in pool if m != chosen]
        tried = []
        last_err = "no model tried"
        for _round in range(2):
            if _round > 0:
                time.sleep(5)
            for model in models:
                if model in tried:
                    continue
                tried.append(model)
                for attempt in range(2):
                    body = {**payload, "model": model}
                    if attempt == 1:
                        body.pop("response_format", None)
                    try:
                        r = requests.post(url, headers=headers, json=body, timeout=120)
                    except requests.RequestException as e:
                        last_err = f"request failed: {e}"
                        time.sleep(2)
                        continue
                    if r.status_code == 200:
                        data = r.json()
                        if data.get("choices"):
                            content = data["choices"][0]["message"].get("content") or ""
                            if content.strip():
                                if json_mode and not _looks_parseable(content):
                                    state.setdefault("skip", {})[model] = now
                                    _save_model_state(state)
                                    last_err = f"{model} returned unparseable output (quarantined {MODEL_QUARANTINE_MIN}min)"
                                    break
                                state.setdefault("skip", {}).pop(model, None)
                                _save_model_state(state)
                                global LAST_MODEL
                                LAST_MODEL = data.get("model", env.get("AI_MODEL", DEFAULT_MODEL))
                                return (
                                    content.strip(),
                                    data.get("usage", {}),
                                    data.get("model", env.get("AI_MODEL", DEFAULT_MODEL)),
                                )
                        last_err = "API returned empty reply"
                        if attempt == 0:
                            time.sleep(3)
                            continue
                    if r.status_code == 429:
                        if attempt == 0:
                            time.sleep(8)
                            continue
                        state.setdefault("skip", {})[model] = now
                        _save_model_state(state)
                        last_err = f"API error 429 (quota) — {model} quarantined {MODEL_QUARANTINE_MIN}min"
                        break
                    if r.status_code in (401, 403):
                        raise RuntimeError(f"API error {r.status_code}: {r.text[:200]}")
                    if r.status_code in (500, 502, 503, 504) and attempt == 0:
                        time.sleep(4)
                        continue
                    if r.status_code == 400 and attempt == 0 and json_mode:
                        last_err = f"API error 400 (retrying without json mode): {r.text[:100]}"
                        continue
                    last_err = f"API error {r.status_code}: {r.text[:120]}"
                    break
                time.sleep(1)
        raise RuntimeError(last_err + f" (tried: {', '.join(tried)})")
    finally:
        spinner.stop()


def chat_call(env: dict[str, str], messages: list[dict[str, str]], max_tokens: int = 300) -> tuple[str, dict, str]:
    return api_call(env, {"messages": messages, "temperature": 0.2, "max_tokens": max_tokens}, json_mode=True)


def parse_tool_reply(text: str) -> dict[str, Any] | None:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return parse_antlr_tool_call(text)
        return parse_antlr_tool_call(text)


def parse_antlr_tool_call(text: str) -> dict[str, Any] | None:
    m = re.search(r"<tool_call>\s*(\w+)", text)
    if not m:
        return None
    tool = {"action": m.group(1)}
    for arg in re.finditer(r"<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>", text, re.S):
        tool[arg.group(1).strip()] = arg.group(2).strip()
    if tool.get("action"):
        return tool
    return None


# ---------------- TOOLS ----------------

class MissingInfo(Exception):
    pass


def ask_bot(question: str) -> str:
    if BOT_MODE:
        raise MissingInfo(question)
    return _safe_input(c(f"  {question}? ", CYAN))


def confirm_action(description: str, danger: bool = False) -> bool:
    if BOT_MODE or not sys.stdin.isatty():
        return True
    answer = _safe_input(c(f"  ⚠ {description}? ", YELLOW) + c("[y/N] > ", GRAY), default="n").lower()
    return answer in ("y", "yes")


_RAR_RUN_RE = re.compile(r"^(?:unrar|7z|7za|7zz|7zr|bsdtar)\s+([xelt])\b")
_EXE_RUN_RE = re.compile(r"(?<![\w.\-/])(?:\./)?[\w.][\w. -]*?\.exe(?:\.[a-z]+)?\b", re.I)
_EXE_SAFE_CMDS = re.compile(
    r"^\s*(?:ls|find|file|cat|head|tail|grep|echo|which|type|rm|cp|mv|stat|du|wc|strings|winecfg|wineboot)\b",
    re.I,
)


def _normalize_shell_cmd(cmd: str) -> str:
    low = cmd.strip()
    m = _RAR_RUN_RE.search(low)
    if m:
        mode, rest = m.group(1), low[m.end():].strip()
        om = re.search(r'-o"?([^"\s]+)"?', rest)
        if om:
            outdir = om.group(1)
            rest = re.sub(r'-o"?[^"\s]+"?\s*', "", rest).strip()
            return f"rar {mode} {rest} {outdir}"
        return f"rar {mode} {rest}"
    if _EXE_RUN_RE.search(low) and not _EXE_SAFE_CMDS.match(low) and "wine" not in low:
        mcd = re.match(r"^(\s*cd\s+[^;&]+&&\s*)(.*)$", low)
        if mcd:
            return mcd.group(1) + "wine " + mcd.group(2)
        return "wine " + low
    return cmd


def run_shell(command: str, timeout: int = 120) -> str:
    r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
    out = (r.stdout or "") + (r.stderr or "")
    if len(out) > MAX_TOOL_REPLY:
        out = out[:MAX_TOOL_REPLY] + "\n...[truncated]"
    out = out.strip() or "(no output)"
    failed = r.returncode != 0 or _output_has_errors(out)
    return ("✗ FAILED (exit %s): %s" % (r.returncode, out)) if failed else ("✓ " + out)


def _keep_tail_summary(out: str) -> str:
    out = out.strip()
    if len(out) <= MAX_TOOL_REPLY or "SUMMARY:" not in out:
        return out[:MAX_TOOL_REPLY] or ""
    keep = out[:MAX_TOOL_REPLY]
    pos = out.rfind("SUMMARY:")
    if pos > MAX_TOOL_REPLY:
        keep += "\n…\n" + _clip(out[pos:], limit=800)
    return keep


_ERROR_MARKERS = (
    "command not found", "no such file or directory", "permission denied",
    "unsupported method", "cannot open", "wrong password", "corrupt",
)


def _output_has_errors(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _ERROR_MARKERS)


def _delegate_blocked(task: str, dir_arg: str) -> bool:
    scrub = re.sub(r"(?:don'?t|do\s+not)\s+touch[^.]*?(?:godot|game)[^.]*\.", "", task, flags=re.I)
    low = scrub.lower()
    blocked = [w for w in ("my-game-my-legacy-2", ".tscn", "win_screen", "boss fight", "boss_fight") if w in low]
    if re.search(r"\b(?:my|our)\s+(?:godot\s+)?game\b", low):
        blocked.append("my/our game")
    if dir_arg and "my-game-my-legacy-2" in dir_arg:
        blocked.append("game dir")
    return bool(blocked)


def run_delegate(task: str, dir_arg: str) -> str:
    if _delegate_blocked(task, dir_arg):
        return "Can't delegate that — the game project (Godot) is off-limits per the boss."
    workdir = Path(os.path.expanduser(dir_arg)) if dir_arg else Path.home()
    emit_progress("⚡ On it — handing this to my senior dev (opencode). Give me a few minutes...")
    env2 = dict(os.environ)
    env2["OPENCODE_CONFIG"] = str(SCRIPT_DIR / "delegate.json")
    binary = shutil.which("opencode")
    if not binary:
        for cand in (Path.home() / ".opencode" / "bin" / "opencode", Path.home() / ".local" / "bin" / "opencode"):
            if cand.exists():
                binary = str(cand)
                break
    if not binary:
        return "opencode CLI not found — install opencode or put it in your PATH."
    cmd = [binary, "run", task + "\n\nIMPORTANT: When you finish, your final message MUST end with a line that starts with 'SUMMARY:' followed by a 2-3 sentence plain-text summary of what you changed and how to use it. The SUMMARY: line is the most important part — always include it even if everything else is short."]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(workdir), env=env2, timeout=900)
    except subprocess.TimeoutExpired:
        return "opencode timed out after 15 minutes."
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 and not out.strip():
        return f"opencode failed (exit {r.returncode})."
    return _keep_tail_summary(out) or "(opencode returned nothing)"


def _clip(text: str, limit: int = 600) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sp = cut.rfind(" ")
    if sp > limit // 2:
        cut = cut[:sp]
    return cut.rstrip(",.;: ") + "…"


def summarize_delegate(out: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", out.strip())
    if not text:
        return "(opencode returned nothing)"
    if "SUMMARY:" in text:
        tail = text.rsplit("SUMMARY:", 1)[1].strip().strip("`")
        keep = []
        for line in tail.split("\n"):
            l = line.strip()
            if not l:
                continue
            if re.match(r"^(?:◆|→|\$|> )", l) or re.match(r"^\d+ in \+\d+ out tokens", l):
                break
            keep.append(l)
        if keep:
            return _clip("\n".join(keep))
    junk = re.compile(r"^(?:\+{1,}|-{1,}|@@|diff |index |---|\+\+\+|◆|\$|→|> )")
    clean = [l.strip() for l in text.split("\n") if l.strip() and not junk.match(l)]
    if clean:
        joined = "\n".join(clean)
        if len(joined) <= 600:
            return joined
        tail = clean[-3:]
        while tail and not re.search(r"[.!?…]\s*$", tail[-1]):
            tail.pop()
        if not tail:
            tail = clean[-1:]
        return "Done. " + _clip("\n".join(tail))
    return _clip(text)


_BROWSER_DECIDE_SYS = (
    "You are operating a real web browser for the user's task. You see the page state "
    "(URL, visible text, and a numbered list of clickable elements and inputs). "
    "Pick ONE next action. Your reply must be ONLY one JSON object — no thinking process, "
    "no explanation, no markdown, no commentary:\n"
    '{"cmd":"click","i":N} click element N\n'
    '{"cmd":"type","i":N,"value":"text"} type into input N (replace ALL its content)\n'
    '{"cmd":"goto","url":"https://..."} navigate to a URL\n'
    '{"cmd":"screenshot"} save a screenshot\n'
    '{"cmd":"answer","text":"final reply to the user"} ONLY when the task is complete, or impossible '
    "(e.g. login wall, captcha, blocked) — then answer honestly with what happened and why."
)


def _browser_decide(task: str, snapshot: str, history: list[str]) -> dict:
    env = load_env()
    hist = "\n".join(f"- {h}" for h in history[-8:]) or "- (none yet)"
    msgs = [
        {"role": "system", "content": _BROWSER_DECIDE_SYS},
        {"role": "user", "content": (
            f"TASK: {task}\n\nACTIONS SO FAR:\n{hist}\n\n"
            f"CURRENT PAGE STATE:\n{snapshot}"
        )},
    ]
    for _attempt in range(6):
        try:
            raw, _usage, _model = api_call(
                env, {"messages": msgs, "temperature": 0.1, "max_tokens": 160},
                json_mode=False,
            )
        except Exception:
            time.sleep(1.0)
            continue
        obj = parse_tool_reply(raw)
        if isinstance(obj, dict) and obj.get("cmd"):
            return obj
        msgs.append({"role": "user", "content": (
            f"You replied with text that is NOT a JSON object (it was: {raw[:200]!r}). "
            "Ignore that. Reply with ONLY one JSON object from the allowed commands — "
            "no reasoning, no explanation, nothing else."
        )})
        time.sleep(0.8)
    lines = snapshot.splitlines()
    where = " ".join(l for l in lines[:2] if l.startswith(("URL:", "TITLE:")))
    return {"cmd": "answer", "text": (
        f"My response brain glitched after that step — but the browser itself is fine. "
        f"Current page: {where or '?'}. Tell me to continue and I'll take it from here."
    )}


def _learn_contact(history: list[dict[str, str]], to: str) -> None:
    to = to.strip()
    if not to or "@" not in to:
        return
    local = to.split("@")[0]
    user_lines = " ".join(str(m.get("content", "")) for m in history if m.get("role") == "user")
    name = None
    m = re.search(r"(?:to|for)\s+([A-Z][A-Za-z]+)", user_lines)
    if m and m.group(1).lower() in local.lower():
        name = m.group(1)
    if not name:
        stem = re.sub(r"\d+", "", local)
        if stem.isalpha() and len(stem) >= 3:
            name = stem[:1].upper() + stem[1:]
    if name:
        save_memory_entry(f"CONTACT: {name}'s email is {to}", replace_similar=True)


def run_tool(env: dict[str, str], tool: dict[str, Any], history: list[dict[str, str]]) -> tuple[str, bool]:
    action = tool.get("action")
    if action == "chat":
        reply = str(tool.get("reply", "")).strip()
        if reply.startswith("{") and '"action"' in reply[:120]:
            try:
                inner = json.loads(reply)
                if isinstance(inner, dict) and inner.get("action") == "chat":
                    reply = str(inner.get("reply", "")).strip()
            except Exception:
                pass
        return reply, True
    if action == "web":
        query = tool.get("query", "").strip()
        if not query:
            query = ask_bot("what should I search for")
        results = web_search(query, 5)
        if not results:
            return "⚠ Search returned nothing right now (backend may be rate-limited). Ask again in a minute.", True
        for i, res in enumerate(results, 1):
            print(c(f"  {i:>2}. ", CYAN) + c(res["title"], BOLD))
            print(c("      " + res["url"], DIM))
        answer = ""
        for _attempt in range(2):
            answer, usage, model = api_call(env, {
                "messages": [
                    {"role": "system", "content": "Summarize the search results to answer the question concisely in plain text. Answer directly — no thinking process, no meta-commentary about the sources." + _price_note(query)},
                    {"role": "user", "content": f"Question: {query}\n\nResults:\n" + "\n".join(f"- {r['title']}: {r['snippet']}" for r in results)},
                ],
                "temperature": 0.3,
                "max_tokens": 300,
            }, json_mode=False)
            ans = (answer or "").strip()
            if _clean_answer(ans):
                break
        print(c("  ── ANSWER ──", CYAN))
        box(answer, GREEN)
        print(tokens_str(usage, model))
        ans = (answer or "").strip()
        if not _clean_answer(ans):
            ans = _source_fallback(results, "The summary model glitched — here's what the sources say:")
        if ans.startswith("{") and '"' in ans[:120]:
            try:
                j = json.loads(ans)
                if isinstance(j, dict):
                    ans = str(j.get("reply") or j.get("answer") or j.get("content") or ans).strip()
            except Exception:
                pass
        srcs = "\n".join(f"{i}. {r['title']} — {r['url']}" for i, r in enumerate(results[:5], 1))
        return (ans or "No results found.") + "\n\nSources:\n" + srcs, True
    if action == "email":
        to = tool.get("to", "").strip()
        subject = tool.get("subject", "").strip()
        topic = tool.get("topic", "").strip()
        fpath = str(tool.get("file") or tool.get("attach") or tool.get("attachment") or "").strip()
        if fpath:
            fpath = os.path.expanduser(fpath)
            if not Path(fpath).exists():
                raise MissingInfo(
                    f"the file {fpath!r} doesn't exist — locate it first (e.g. with a shell find) and use the real absolute path"
                )
            if Path(fpath).stat().st_size > 25 * 1024 * 1024:
                return "That file is too big to attach to an email (email limit is ~25MB). Upload it to Google Drive using the browser, then email the share link instead.", True
        if not to:
            to = ask_bot("who is it to")
        if not subject:
            subject = ask_bot("subject")
        if not topic:
            topic = ask_bot("what should it say")
        if not to or not subject or not topic:
            return "Email cancelled — missing details.", True
        if email_flow(env, to, subject, topic, file=fpath or None) == "sent":
            _learn_contact(history, to)
            return "Email sent successfully" + (f" with attachment {os.path.basename(fpath)}." if fpath else "."), True
        return "Email cancelled by user.", True
    if action == "sort":
        path = tool.get("path", "").strip()
        if not path:
            path = ask_bot("which folder")
        cmd_sort(argparse.Namespace(path=os.path.expanduser(path), dry_run=False), env)
        return "", True
    if action == "remember":
        text = tool.get("text", "").strip()
        if text:
            save_memory_entry(text)
            return "Got it, I'll remember that.", True
        return "What should I remember?", True
    if action == "memory":
        notes = load_memory()
        if not notes:
            return "I don't have any notes stored yet.", True
        return "My notes:\n- " + "\n- ".join(notes), True
    if action == "forget":
        forget_memory()
        return "Memory cleared.", True
    if action == "shell":
        command = _normalize_shell_cmd(tool.get("command", "").strip())
        if not command:
            return "No command given.", False
        print(c("  $ ", CYAN) + c(command, BOLD))
        if not confirm_action(f"run: {command}", danger=True):
            return "User cancelled.", False
        try:
            out = run_shell(command)
        except subprocess.TimeoutExpired:
            out = "(command timed out after 120s)"
        print(c("  ── OUTPUT ──", CYAN))
        print(c(out[:1200], GRAY))
        return out, False
    if action == "read":
        path = Path(os.path.expanduser(tool.get("path", "")))
        if not path.exists():
            return f"File not found: {path}", False
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"Could not read {path}: {e}", False
        if len(content) > 4000:
            content = content[:4000] + "\n...[truncated]"
        print(c(f"  ── {path} ──", CYAN))
        print(c(content[:2000], GRAY))
        return content, False
    if action == "write":
        path = Path(os.path.expanduser(tool.get("path", "")))
        content = tool.get("content", "")
        print(c("  ✎ write to: ", YELLOW) + c(str(path), BOLD))
        if not confirm_action(f"write {len(content)} chars to {path}"):
            return "User cancelled.", False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return f"Saved to {path}", False
        except OSError as e:
            return f"Could not write {path}: {e}", False
    if action == "list":
        path = Path(os.path.expanduser(tool.get("path", ".")))
        if not path.is_dir():
            return f"Not a directory: {path}", False
        try:
            names = sorted(p.name for p in path.iterdir())
        except OSError as e:
            return f"Could not list {path}: {e}", False
        listing = "\n".join(name + ("/" if (path / name).is_dir() else "") for name in names[:80]) if names else "(empty folder)"
        print(c(f"  ── {path}/ ──", CYAN))
        print(c(listing[:2000], GRAY))
        return listing, False
    if action == "self":
        return _run_self_tool(tool)
    if action == "delegate":
        task = tool.get("task", "").strip()
        if not task:
            task = ask_bot("what should I delegate to opencode")
        if not task:
            return "No task given to delegate.", True
        return summarize_delegate(run_delegate(task, tool.get("dir") or "")), True
    if action == "skill":
        propose = tool.get("propose")
        if isinstance(propose, list) and propose:
            lines = []
            for i, s in enumerate(propose, 1):
                if isinstance(s, dict):
                    lines.append(f"{i}. {s.get('name', '?')} — {s.get('purpose', s.get('description', ''))}")
                else:
                    lines.append(f"{i}. {s}")
            text = "Skills I want to build for myself:\n" + "\n".join(lines)
            emit_progress("⚡ Proposing new skills for myself...")
            build_text = (
                "Create opencode skills. For each skill: create a folder "
                "~/.config/opencode/skills/<name>/ with a SKILL.md (frontmatter: name and description; "
                "body: concise workflow instructions). Do NOT touch any game project files.\n"
                + "\n".join(
                    f"- {s.get('name') if isinstance(s, dict) else s}: "
                    f"{s.get('purpose', s.get('description', '')) if isinstance(s, dict) else ''}"
                    for s in propose
                )
            )
            emit_progress("⚡ Building them now — delegating to opencode...")
            return summarize_delegate(run_delegate(build_text, "")), True
        return "Say 'build yourself new skills' and I'll propose specific ones.", True
    if action == "organize":
        path = tool.get("path", "").strip()
        if not path:
            path = ask_bot("which folder")
        cmd_organize(argparse.Namespace(path=os.path.expanduser(path), dry_run=False), env)
        return "", True
    if action == "research":
        query = tool.get("query", "").strip()
        if not query:
            query = ask_bot("what should I research")
        n = int(tool.get("n", 8) or 8)
        results = web_research(query, n)
        if not results:
            return "⚠ Search returned nothing right now (backend may be rate-limited). Ask again in a minute.", True
        for i, res in enumerate(results, 1):
            print(c(f"  [{i}] ", CYAN) + c(res["title"], BOLD))
            print(c("      Source: " + res["url"], DIM))
            if res.get("snippet"):
                print(c("      " + res["snippet"][:150], GRAY))
        answer = ""
        for _attempt in range(2):
            answer, usage, model = api_call(env, {
                "messages": [
                    {"role": "system", "content": "Summarize the findings to answer the question. Cite sources by their [n] number from the results. Answer directly — no thinking process, no meta-commentary." + _price_note(query)},
                    {"role": "user", "content": f"Question: {query}\n\nResults:\n" + "\n".join(f"[{i}] {r['title']}: {r['url']} — {r['snippet']}" for i, r in enumerate(results, 1))},
                ],
                "temperature": 0.3,
                "max_tokens": 400,
            }, json_mode=False)
            ans = (answer or "").strip()
            if _clean_answer(ans):
                break
        print(c("  ── SUMMARY (cited) ──", CYAN))
        box(answer, GREEN)
        print(tokens_str(usage, model))
        ans = (answer or "").strip()
        if not _clean_answer(ans):
            ans = _source_fallback(results, "The summary model glitched — here's what the sources say:")
        srcs = "\n".join(f"{i}. {r['title']} — {r['url']}" for i, r in enumerate(results[:5], 1))
        return (ans or "No results found.") + "\n\nSources:\n" + srcs, True
    if action == "brainstorm":
        query = tool.get("query", "").strip()
        if not query:
            query = ask_bot("what should I brainstorm")
        if not query:
            return "Brainstorm cancelled — no topic given.", True
        answer, usage, model = api_call(
            env,
            {
                "messages": [
                    {"role": "system", "content": "You are jorge's sharp brainstorming engine. Produce a structured brainstorm for the topic. Format EXACTLY like this (plain text, no markdown headers):\nCORE IDEAS\n- <idea>: <one-line why it works>\n...\nANGLES & APPROACHES\n- <angle>: <one line>\n...\nWILDCARDS\n- <wild idea>: <one line>\n...\nNEXT STEPS\n- <action>: <who/what/why one line>\n...\nAim for 8-12 ideas total, be specific and practical, no fluff."},
                    {"role": "user", "content": f"Topic: {query}"},
                ],
                "temperature": 0.9,
                "max_tokens": 900,
            },
            json_mode=False,
        )
        print(c("  ── BRAINSTORM ──", CYAN) + tokens_str(usage, model))
        box(answer, GREEN)
        return (answer or "No ideas came out — try again?").strip(), True
    if action == "chess":
        query = tool.get("query", "").strip()
        if not query:
            query = ask_bot("what chess position should I analyze")
        if not query:
            return "Chess cancelled — no position given.", True
        print(c("  ♟ analyzing with stockfish…", CYAN))
        return chess_bot.analyze(query), True
    if action == "chess_vs":
        elo = int(re.search(r"\d+", str(tool.get("query", ""))).group()) if re.search(r"\d+", str(tool.get("query", ""))) else 1200
        print(c("  ♟ starting a game…", CYAN))
        return chess_bot.new_game(str(tool.get("user", "default")), elo, str(tool.get("side") or "white")), True
    if action == "chess_move":
        print(c("  ♟ jorge thinking…", CYAN))
        return chess_bot.play_move(str(tool.get("user", "default")), str(tool.get("query", "") or "")), True
    if action == "chess_challenge":
        return chess_bot.chess_challenge(str(tool.get("user", "default")), str(tool.get("query", "") or "")), True
    if action == "chess_accept":
        return chess_bot.chess_accept(str(tool.get("user", "default"))), True
    if action == "chess_decline":
        return chess_bot.chess_decline(str(tool.get("user", "default"))), True
    if action == "email_draft":
        to = tool.get("to", "").strip()
        subject = tool.get("subject", "").strip()
        topic = tool.get("topic", "").strip()
        tone = tool.get("tone") or "professional"
        if not to:
            to = ask_bot("who is it to")
        if not topic:
            topic = ask_bot("what should the email say")
        if not to or not topic:
            return "Email draft cancelled — missing details.", True
        context = tool.get("context") or ""
        recall = bool(tool.get("recall"))
        if recall:
            context = (context + "\n" if context else "") + "Relevant recent conversation:\n" + _recent_context()
        if tone not in EMAIL_TONES:
            tone = "professional"
        try:
            guide = EMAIL_TONE_GUIDE[tone]
            body, usage, model = api_call(
                env,
                {
                    "messages": [
                        {"role": "system", "content": "You are a skilled email writer. Write only the email body."},
                        {"role": "user", "content": f"Recipient: {to}\nTone: {tone} ({guide})\nSubject: {subject or '(suggest one)'}\nContent: {topic}\n\nContext: {context or '(none)'}"},
                    ],
                    "temperature": 0.6,
                    "max_tokens": 600,
                },
                json_mode=False,
            )
        except RuntimeError as e:
            return f"Email draft failed: {e}", True
        print(c("  ── DRAFT ──", CYAN) + tokens_str(usage, model))
        box(body, GREEN)
        if confirm_action(f"send this {tone} email to {to}"):
            send_email(env, to, subject or "(no subject)", body)
            return f"Sent {tone} email to {to}.", True
        return f"Here's the {tone} draft (not sent): {body[:300]}", True
    if action == "monitor":
        try:
            top = int(tool.get("top", 5) or 5)
            cpu = float(tool.get("cpu", 90) or 90)
            mem = float(tool.get("mem", 85) or 85)
            disk = float(tool.get("disk", 90) or 90)
        except (TypeError, ValueError):
            top, cpu, mem, disk = 5, 90.0, 85.0, 90.0
        if psutil is None:
            return "psutil not installed — run: pip install psutil", True
        stats = collect_system_stats()
        print(c("  " + _fmt_stats(stats).replace("\n", "\n  "), GRAY) + "\n")
        print(c("  top processes:", BOLD))
        for p in top_processes(top):
            print(c(f"  {p.get('name', '?')}", BOLD) + c(f"  pid={p.get('pid')}", DIM) +
                  c(f"  cpu={round(p.get('cpu_percent') or 0, 1)}%  mem={round(p.get('memory_percent') or 0, 1)}%", GRAY))
        print()
        for a in check_alerts({"cpu": cpu, "mem": mem, "disk": disk}):
            color = RED if a.startswith("⚠") else GREEN
            print(c("  " + a, color))
        summary = " ".join(check_alerts({"cpu": cpu, "mem": mem, "disk": disk}))
        return f"Monitored: {_fmt_stats(stats).splitlines()[0]}, {_fmt_stats(stats).splitlines()[1]}, {_fmt_stats(stats).splitlines()[2]}. {summary}", True
    if action == "delegate_breakdown":
        task = tool.get("task", "").strip()
        if not task:
            task = ask_bot("what is the complex task")
        if not task:
            return "No task given.", True
        steps = break_down_task(task, int(tool.get("steps", 5) or 5))
        print(c("  Task breakdown:", BOLD))
        for i, s in enumerate(steps, 1):
            print(c(f"  {i}. ", CYAN) + c(s, BOLD))
        print()
        if confirm_action("delegate the full task to opencode now") or BOT_MODE:
            emit_progress("⚡ Delegating to my senior dev (opencode)...")
            return summarize_delegate(run_delegate(task, tool.get("dir") or "")), True
        return "Here's the breakdown (not delegated yet): " + " | ".join(f"{i}. {s}" for i, s in enumerate(steps, 1)), True
    if action == "browser":
        task = tool.get("task", "").strip()
        if not task:
            task = ask_bot("what should I do in the browser")
        if not task:
            return "No browser task given.", True
        headed = bool(re.search(r"\bheaded\s*$", task, re.I))
        if headed:
            task = re.sub(r"\s*headed\s*$", "", task, flags=re.I).strip()
        return browser_task(
            task,
            start_url=tool.get("url", ""),
            decide=_browser_decide,
            ask=ask_bot,
            progress=emit_progress,
            headless=not headed,
        ), True
    if action == "browser_creds":
        site = tool.get("site", "").strip()
        email = tool.get("email", "").strip()
        password = tool.get("password", "").strip()
        if not (site and email and password):
            return "browser_creds needs site, email and password.", True
        return save_credentials(site, email, password), True
    if action == "browser_2fa":
        site = tool.get("site", "").strip()
        code = tool.get("code", "").strip()
        if not (site and code):
            return "browser_2fa needs site and code.", True
        return save_2fa(site, code), True
    return "I didn't catch that — try again?", True


def _run_self_tool(tool: dict[str, Any]) -> tuple[str, bool]:
    task = (tool.get("task") or "status").strip()
    own = SCRIPT_DIR / "assistant.py"
    if task == "status":
        lines = ["I am jorge (the assistant).", f"Source: {own}"]
        for f in ("assistant.py", "memory.txt", "conversations.jsonl", ".env", "requirements.txt"):
            p = SCRIPT_DIR / f
            lines.append(f"{f}: {'exists' if p.exists() else 'missing'}")
        return "\n".join(lines), True
    if task in ("view", "read"):
        try:
            content = own.read_text(encoding="utf-8")
        except OSError as e:
            return f"Could not read own source: {e}", True
        lines = content.splitlines()
        find = tool.get("find")
        if find:
            hits = []
            for i, ln in enumerate(lines, 1):
                if find in ln:
                    start = max(1, i - 3)
                    end = min(len(lines), i + 3)
                    hits.append(f"...lines {start}-{end}...")
                    hits.extend(f"{j:>4} | {lines[j - 1]}" for j in range(start, end + 1))
                    hits.append("...")
            shown = "\n".join(hits[:60]) if hits else f"(no lines contain {find!r} — file has {len(lines)} lines)"
            print(c(f"  ── {own} (self, find {find!r}) ──", CYAN))
            print(c(shown[:2500], GRAY))
            return shown, False
        print(c(f"  ── {own} (self) ──", CYAN))
        print(c(content[:2000], GRAY))
        return content, False
    if task in ("edit", "patch"):
        replace = tool.get("replace")
        edits = tool.get("edits") or []
        if not isinstance(edits, list) or not (replace is not None or edits):
            return "self edit: provide 'edits' (list of old/new pairs) or 'replace' (full source).", True
        try:
            source = own.read_text(encoding="utf-8")
        except OSError as e:
            return f"Could not read own source: {e}", True
        if replace is not None:
            if not isinstance(replace, str) or "def " not in replace:
                return "self edit: 'replace' must be the full new source of assistant.py.", True
            source = replace
        else:
            for ed in edits:
                if not isinstance(ed, dict) or not isinstance(ed.get("old"), str) or not isinstance(ed.get("new"), str):
                    return "self edit: each edit needs string 'old' and 'new'.", True
                old = ed["old"]
                if old not in source:
                    norm = re.sub(r"\s+", " ", old).strip()
                    match = re.search(re.escape(norm.replace(" ", r"\s+")), source)
                    if match:
                        old = match.group(0)
                    else:
                        return f"self edit: old text not found: {old[:80]!r}", True
                source = source.replace(old, ed["new"], 1)
        try:
            compile(source, str(own), "exec")
        except SyntaxError as e:
            return f"self edit ABORTED — syntax error ({e}). Nothing was saved.", True
        backup = Path(str(own) + ".bak")
        shutil.copy(own, backup)
        own.write_text(source, encoding="utf-8")
        print(c("  ✓ self code updated (backup kept at assistant.py.bak)", BOLD, GREEN))
        return 'Self code updated. It takes effect the next time "jorge" is launched.', True
    if task in ("debug", "check"):
        res = subprocess.run([sys.executable, "-m", "py_compile", str(own)], capture_output=True, text=True)
        return ("✓ assistant.py compiles clean" if res.returncode == 0 else "✗ compile error:\n" + res.stderr[:500]), True
    return f"Unknown self task: {task}. Use status, view, edit, or debug.", True


SKILL_HINTS = ("improv", "build", "new skill", "new skills", "update", "upgrade", "add a skill", "make yourself", "better at", "learn")


def detect_skill_intent(line: str) -> bool:
    low = line.lower()
    if "skill" not in low:
        return False
    return any(h in low for h in SKILL_HINTS)


DEFAULT_SKILL_PROPOSAL = (
    "Skills I want to build for myself:\n"
    "1. email-polish — write cleaner, more professional emails faster\n"
    "2. downloads-sorter — organize your Downloads folder into categories\n"
    "3. whatsapp-delegate — hand off bigger coding tasks to opencode\n"
    "4. memory-notes — remember more about you and your projects\n"
    "5. self-edit — improve my own code safely and check it compiles"
)


def action_key(tool: dict[str, Any]) -> str:
    parts = [str(tool.get("action", ""))]
    for k in ("command", "path", "query", "text", "to", "subject", "topic"):
        v = tool.get(k)
        if v:
            parts.append(re.sub(r"\s+", "", str(v))[:120])
    return "|".join(parts)


def summarize_tool(tool: dict[str, Any], reply: str, prev: str | None) -> str:
    action = tool.get("action", "")
    if action == "shell":
        cmd = tool.get("command", "").strip()
        out = reply.replace("\n", " ").strip()
        if out and "(no output" not in out:
            return f"Done — ran: {cmd} ({out[:120]})"
        return f"Done — ran: {cmd}"
    if reply and reply.strip():
        return reply.strip()
    return prev or "Done."


SELF_INDEX_FILE = SCRIPT_DIR / "self_index.txt"


def build_self_index() -> str:
    src_path = SCRIPT_DIR / "assistant.py"
    try:
        src = src_path.read_text(encoding="utf-8")
    except Exception:
        return ""
    funcs = []
    for m in re.finditer(r"^def (\w+)\((.*?)\)\s*(?:->[^:]+)?:", src, re.M | re.S):
        name, sig = m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()[:110]
        doc = ""
        dm = re.match(r"\n\s*(?:\"\"\"|''')(.*?)(?:\"\"\"|''')", src[m.end():], re.S)
        if dm:
            doc = re.sub(r"\s+", " ", dm.group(1)).strip()[:90]
        funcs.append(f"def {name}({sig})" + (f" — {doc}" if doc else ""))
    consts = []
    for cm in re.finditer(r"^([A-Z][A-Z0-9_]{2,})\s*=\s*(.+)$", src, re.M):
        val = re.sub(r"\s+", " ", cm.group(2)).strip()
        if len(val) > 60:
            val = val[:60] + "..."
        consts.append(f"{cm.group(1)} = {val}")
    parts = ["FUNCTIONS:", "\n".join(funcs[:130]), "CONSTANTS:", "\n".join(consts[:40])]
    return "\n".join(parts)


def get_self_index() -> str:
    src_path = SCRIPT_DIR / "assistant.py"
    try:
        if SELF_INDEX_FILE.exists() and SELF_INDEX_FILE.stat().st_mtime >= src_path.stat().st_mtime:
            return SELF_INDEX_FILE.read_text(encoding="utf-8")[:4500]
    except Exception:
        pass
    text = build_self_index()
    try:
        SELF_INDEX_FILE.write_text(text, encoding="utf-8")
    except Exception:
        pass
    return text[:4500]


def _build_chat_system() -> str:
    system = (
        CHAT_SYSTEM
        .replace("{HOME}", os.path.expanduser("~"))
        .replace("{USER}", os.environ.get("USER", "user"))
        .replace("{SCRIPT_DIR}", str(SCRIPT_DIR))
    )
    mem = load_memory()
    if mem:
        system += "\nMemory notes about the user:\n- " + "\n- ".join(mem)
    lessons = load_mistakes()
    if lessons:
        system += (
            "\n\nLESSONS LEARNED FROM PAST MISTAKES — NEVER repeat these. "
            "If a lesson matches the current situation, follow the lesson:\n- "
            + "\n- ".join(lessons)
        )
    system += (
        "\n\nYOUR OWN CODE (self-knowledge index — always up to date with your current source, "
        "refreshed whenever your code changes). Use it to answer questions about yourself and "
        "to make precise self edits:\n" + get_self_index()
    )
    if _load_plan_state()["mode"]:
        system += (
            "\n\nPLAN MODE IS ON: reply with your normal JSON actions. Side-effecting actions "
            "(shell, file write, email, sort/organize, delegate, skill build, forget) will be "
            "queued into a plan for the user's approval instead of executed — that is expected, "
            "do not tell the user you're skipping or cancelling. After queuing, finish with a "
            "short chat action telling the user you have a plan ready for their approval."
        )
    return system


def _wrap_reply(env: dict[str, str], msgs: list[dict[str, str]]) -> tuple[str, dict, str]:
    return chat_call(
        env,
        msgs + [{"role": "user", "content": "(user is waiting for you) Acknowledge what you just found or did in 1-2 short sentences, then ask what they want to do next."}],
        max_tokens=200,
    )


def _answer_context(line: str, history: list[dict[str, str]]) -> str:
    plain = re.sub(r"[.!?,\s]+", " ", line).strip().lower()
    first = plain.split(" ", 1)[0] if plain else ""
    if not (plain in ("do it", "go ahead", "cancel", "don't", "dont", "stop", "abort") or first in ("yes", "yeah", "yep", "yup", "sure", "ok", "okay", "kk", "no", "nope", "nah", "nay", "y", "n")):
        return line
    for msg in reversed(history):
        if msg["role"] != "assistant":
            continue
        content = msg["content"].strip()
        lower = content.lower()
        if ("?" in content or any(w in lower for w in ("do you want", "want me to", "shall i", "should i", "can i", "proceed", "confirm", "are you sure", "okay to", "ok to"))):
            return f"{line.strip()} (replying to your last question: \"{content[:280]}\")"
    return line


def _last_file_list(history: list[dict[str, str]]) -> tuple[str, str]:
    file_re = re.compile(r"^[^ \t()\[\]]+\.[A-Za-z0-9]{1,6}$")
    path_re = re.compile(r"/(?:[\w.&+()\-]+/)+[\w.&+()\-]+")
    base = ""
    for msg in reversed(history):
        if msg["role"] == "assistant":
            found = path_re.findall(msg["content"])
            if found:
                base = found[-1].rstrip("/.")
                break
    for msg in reversed(history):
        content = msg["content"]
        if msg["role"] == "user" and not content.startswith("[tool result"):
            continue
        hits = [l.strip() for l in content.splitlines() if file_re.match(l.strip())]
        if len(hits) >= 3:
            return "\n".join(hits[:200]), base
    return "", base


def _selection_context(line: str, history: list[dict[str, str]]) -> str:
    if not re.search(r"\b(delete|remove|all|those|these|them|first|last|keep)\b", line.lower()):
        if not re.search(r"\b[^ \t]+\.[A-Za-z0-9]{1,6}\b", line):
            return line
    listing, base = _last_file_list(history)
    if not listing:
        return line
    if base and not listing.split("\n")[0].startswith("/"):
        listing = "\n".join(f"{base}/{f}" for f in listing.split("\n"))
    return f"{line.strip()}\n\n(The files you just listed and asked about — act on exactly these, do not ask again:\n{listing[:2000]}\n)"


def brain_reply(env: dict[str, str], line: str, history: list[dict[str, str]], user: str = "default") -> tuple[str, list[dict[str, str]]]:
    low = line.strip().lower()
    m = re.match(r"plan mode (on|off)\b", low)
    if m:
        st = _load_plan_state()
        st["mode"] = m.group(1) == "on"
        if not st["mode"]:
            st["pending"] = []
            st["plans"] = []
        _save_plan_state(st)
        msg = (
            "Plan mode is ON — I'll propose a plan and wait for your approval before doing anything."
            if st["mode"]
            else "Plan mode is OFF — I'll act directly again."
        )
        history.append({"role": "assistant", "content": msg})
        history = history[-MAX_HISTORY:]
        append_conversation({"role": "assistant", "content": msg})
        print(c("  > ", CYAN) + msg)
        return msg, history
    st = _load_plan_state()
    _APPROVE_RE = re.compile(
        r"\b(?:yes|ya|yep|yeah|yup|proceed|approve|confirm|sure|okay|ok|do it|sounds good|"
        r"use delegation|delegate it|go ahead|go for it)\b|^go\b", re.I)
    if st["pending"]:
        if _APPROVE_RE.search(low):
            tools, steps = st["pending"], st["plans"]
            st["pending"] = []
            st["plans"] = []
            _save_plan_state(st)
            lines = []
            for i, (tool, step) in enumerate(zip(tools, steps), 1):
                try:
                    reply, _done = run_tool(env, tool, history)
                    lines.append(f"{i}. {step} — {reply.strip()[:200]}")
                except MissingInfo as mi:
                    lines.append(f"{i}. {step} — need more info: {mi}")
                    break
                except Exception as e:
                    lines.append(f"{i}. {step} — error: {e}")
                    continue
            msg = "Plan approved — executing:\n" + "\n".join(lines)
            history.append({"role": "assistant", "content": msg})
            history = history[-MAX_HISTORY:]
            append_conversation({"role": "assistant", "content": msg})
            print(c("  > ", CYAN) + msg)
            return msg, history
        if re.match(r"^(?:no|nope|cancel|stop|don'?t|never ?mind|forget it|abort)\b", low):
            st["pending"] = []
            st["plans"] = []
            _save_plan_state(st)
            msg = "Plan cancelled — nothing was executed."
            history.append({"role": "assistant", "content": msg})
            history = history[-MAX_HISTORY:]
            append_conversation({"role": "assistant", "content": msg})
            print(c("  > ", CYAN) + msg)
            return msg, history
        st["pending"] = []
        st["plans"] = []
        _save_plan_state(st)
    if re.search(
        r"\b(?:wrong|incorrect)\b|that'?s not|not what i|i (?:said|meant|asked for)|"
        r"don'?t (?:do|send|delete|run)|stop doing|why did you|you (?:forgot|missed|should'?ve|messed|broke)|"
        r"never (?:do|send|delete|run)|my name is not",
        low,
    ):
        last = next((m["content"] for m in reversed(history) if m["role"] == "assistant"), "")
        save_mistake(
            f'Lesson: user corrected me — "{line.strip()[:150]}"'
            + (f' (after my earlier: "{last[:200]}")' if last else "")
            + ". Do not repeat this mistake."
        )
    line = _answer_context(line, history)
    line = _selection_context(line, history)
    history.append({"role": "user", "content": line})
    history = history[-MAX_HISTORY:]
    append_conversation({"role": "user", "content": line})
    m = re.match(r"^(?:email|send(?: an)? email|send)\s+([\w.+-]+@[\w.-]+\.[\w.]+)\s*(.*)$", line, re.I)
    if m:
        to = m.group(1)
        rest = m.group(2).strip()
        subject, topic = "(no subject)", rest or "(no content)"
        if rest:
            try:
                raw, _u, _m = chat_call(
                    env,
                    [
                        {"role": "system", "content": 'Extract the subject and content instructions for an email from the user\'s request. Reply with ONLY JSON: {"subject": "...", "topic": "..."}. Subject: short (max 8 words). Topic: a clear instruction of what the email should say.'},
                        {"role": "user", "content": rest},
                    ],
                    max_tokens=150,
                )
                j = json.loads(raw)
                subject = str(j.get("subject") or subject)[:80]
                topic = str(j.get("topic") or topic)
            except Exception:
                subject = f"About {' '.join(rest.split()[:6])}"
        reply, done = run_tool(env, {"action": "email", "to": to, "subject": subject, "topic": topic}, history)
        final_assistant_reply = reply.strip()
        print(c("  > ", CYAN) + final_assistant_reply)
        history.append({"role": "assistant", "content": final_assistant_reply})
        history = history[-MAX_HISTORY:]
        append_conversation({"role": "assistant", "content": final_assistant_reply})
        return final_assistant_reply, history
    chess_vs = re.search(
        r"\bchess\b[^.!?\n]*(?:\b(?:vs|versus|against|play|game|match|challenge|fight)\b)|\b(?:vs|versus)\b[^.!?\n]*\bchess\b",
        line,
        re.I,
    )
    if chess_vs:
        elo_m = re.search(r"\b(\d{3,4})\b", line)
        elo = elo_m.group(1) if elo_m else "1200"
        side = "black" if re.search(r"\b(?:you|u)\s+(?:start|move first|go first|begin)\b", line, re.I) else "white"
        reply, done = run_tool(env, {"action": "chess_vs", "query": elo, "user": user, "side": side}, history)
        final_assistant_reply = reply.strip()
        print(c("  > ", CYAN) + final_assistant_reply)
        history.append({"role": "assistant", "content": final_assistant_reply})
        history = history[-MAX_HISTORY:]
        append_conversation({"role": "assistant", "content": final_assistant_reply})
        return final_assistant_reply, history
    move_re = re.compile(
        r"^(?:[a-h][1-8](?:[a-h][1-8])?(?:=[qrbn])?|[kqrbn][a-h1-8x=+@-]*|O-O(?:-O)?|0-0(?:-0)?|resign|surrender|gg|quit|board|fen|show|start)\s*$",
        re.I,
    )
    if chess_bot.has_game(user) and move_re.match(line.strip()):
        reply, done = run_tool(env, {"action": "chess_move", "query": line.strip(), "user": user}, history)
        final_assistant_reply = reply.strip()
        print(c("  > ", CYAN) + final_assistant_reply)
        history.append({"role": "assistant", "content": final_assistant_reply})
        history = history[-MAX_HISTORY:]
        append_conversation({"role": "assistant", "content": final_assistant_reply})
        return final_assistant_reply, history
    final_assistant_reply: str | None = None
    try:
        msgs = [{"role": "system", "content": _build_chat_system()}] + history
        force_skill = detect_skill_intent(line)
        if force_skill:
            msgs[0]["content"] = msgs[0]["content"] + (
                "\nMANDATORY INSTRUCTION: the user asked to build/improve your skills. "
                'You MUST reply with ONLY {"action":"skill","propose":[{"name":"<kebab-case-name>","purpose":"<one-line purpose>"}]} '
                "with 3-5 specific skills for YOURSELF, based on what the user uses you for. "
                "Do NOT use action email, shell, chat, web, or anything else."
            )
        last_actions = []
        completion_summary: str | None = None
        last_tool_failed: str | None = None
        for _step in range(8):
            raw, usage, model = chat_call(env, msgs, max_tokens=900)
            tool = parse_tool_reply(raw)
            if tool is None:
                if _step <= 2:
                    for msg in reversed(msgs):
                        if msg["role"] == "user" and msg["content"].startswith("[tool result"):
                            msg["content"] = msg["content"][:500] + "…"
                            break
                    msgs.append({"role": "user", "content": f"You replied with text that is NOT a JSON object (it was: {raw[:200]!r}). Ignore that. Reply with ONLY one valid JSON object from the allowed actions — no reasoning, no explanation, nothing else."})
                    continue
                print(c(f"  [debug] garbled raw: {raw[:300]!r}", DIM))
                if force_skill:
                    final_assistant_reply = DEFAULT_SKILL_PROPOSAL
                elif last_actions:
                    try:
                        wrap, usage, model = chat_call(
                            env,
                            msgs + [{"role": "user", "content": f"The user's message was: {line!r}. You got interrupted mid-task. Tell the user in 1-2 short plain sentences what you have done so far and what you were about to do next, addressing THEIR message. No JSON, no talk about JSON or output format — just a quick status update about their request."}],
                            max_tokens=250,
                        )
                        final_assistant_reply = wrap.strip()[:600] or "I got interrupted mid-task — what should I do next?"
                        if final_assistant_reply.startswith("{") and '"action"' in final_assistant_reply[:120]:
                            final_assistant_reply = completion_summary or "I got interrupted mid-task — what should I do next?"
                    except Exception:
                        final_assistant_reply = completion_summary or "I got interrupted mid-task — what should I do next?"
                else:
                    final_assistant_reply = completion_summary or "(sorry — that reply got garbled. try rephrasing.)"
                print(c("  > ", CYAN) + final_assistant_reply)
                print(tokens_str(usage, model))
                break
            if force_skill and tool.get("action") != "skill":
                msgs.append({"role": "user", "content": "Wrong action. The user asked about improving your skills — you MUST reply with action skill + a specific 'propose' list. Nothing else."})
                continue
            key = action_key(tool)
            if key in last_actions:
                last_actions.append(key)
                if last_actions.count(key) >= 3:
                    st2 = _load_plan_state()
                    if st2["mode"] and st2["pending"]:
                        steps2 = "\n".join(f"{i}. {s}" for i, s in enumerate(st2["plans"], 1))
                        final_assistant_reply = "PLAN MODE — here's my plan:\n" + steps2 + "\n\nApprove? (yes / go / proceed — or no to cancel)"
                    else:
                        final_assistant_reply = completion_summary or "I kept repeating the same action, so I stopped — nothing new ran. What should I do next?"
                    print(c("  > ", CYAN) + final_assistant_reply)
                    break
                msgs.append({"role": "user", "content": 'You already ran that action and it succeeded. The task is done — reply with ONLY {"action":"chat","reply":"<a short completion message to the user>"} confirming what you did. Do not run any more actions.'})
                continue
            last_actions.append(key)
            st = _load_plan_state()
            if st["mode"] and tool.get("action") in PLAN_GATED:
                step = _plan_step(tool)
                if step:
                    st["pending"].append(tool)
                    st["plans"].append(step)
                    _save_plan_state(st)
                    msgs.append({"role": "user", "content": f"[tool result] queued for plan approval (plan mode): {step}"})
                    continue
            try:
                if tool.get("action") in ("chess", "chess_vs", "chess_move"):
                    tool["user"] = user
                reply, done = run_tool(env, tool, history)
            except MissingInfo as mi:
                final_assistant_reply = f"I need more info: {mi}."
                break
            except Exception as e:
                final_assistant_reply = f"⚠ {e}"
                break
            completion_summary = summarize_tool(tool, reply, completion_summary)
            if tool.get("action") == "shell" and reply.startswith("✗"):
                last_tool_failed = reply
            if done:
                show = reply.strip()
                if show and tool.get("action") == "shell" and last_tool_failed:
                    show = f"That failed — {last_tool_failed[:400]}"
                elif show and tool.get("action") == "shell" and len(show.splitlines()) > 3:
                    try:
                        wrap, usage, model = chat_call(
                            env,
                            msgs + [{"role": "user", "content": f"[A command you ran just finished. Command:\n{tool.get('command','')}\n\nOutput:\n{show[:1500]}\n\nTell the user the result in 1-2 short plain sentences with the important facts (like how many files were deleted or moved). If the output shows errors or failure, say so honestly — NEVER claim success. Do NOT paste the raw output.]"}],
                            max_tokens=250,
                        )
                        if wrap.strip():
                            show = wrap.strip()
                    except Exception:
                        pass
                if show:
                    print(c("  > ", CYAN) + show)
                    print(tokens_str(usage, model))
                    final_assistant_reply = show
                else:
                    wrap, usage, model = _wrap_reply(env, msgs)
                    if wrap.strip() and not (wrap.lstrip().startswith("{") and '"action"' in wrap[:120]):
                        print(c("  > ", CYAN) + wrap.strip())
                        print(tokens_str(usage, model))
                        final_assistant_reply = wrap.strip()
                    else:
                        final_assistant_reply = completion_summary or "(no reply)"
                break
            msgs.append({"role": "user", "content": f"[tool result for my last action]\n{reply[:MAX_TOOL_REPLY]}"})
        else:
            print(c("  > ", CYAN) + (completion_summary or "(no reply)"))
            final_assistant_reply = completion_summary or "(no reply)"
    except Exception as e:
        print(c(f"  ✗ {e}", RED))
        final_assistant_reply = final_assistant_reply or f"⚠ {e}"
    if last_tool_failed and final_assistant_reply and re.search(
        r"\b(?:successfully|completed|done|extracted|finished|all ok)\b", final_assistant_reply, re.I
    ):
        final_assistant_reply = f"That didn't work — {last_tool_failed[:400]}"
    st = _load_plan_state()
    if st["mode"] and st["pending"]:
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(st["plans"], 1))
        final_assistant_reply = "PLAN MODE — here's my plan:\n" + steps + "\n\nApprove? (yes / go / proceed — or no to cancel)"
    if final_assistant_reply and not any(k in last_actions for k in ("research",)):
        if _wants_research(line) and (
            (not last_actions or set(last_actions) <= {"chat", "delegate_breakdown"})
            or (
                any(k in last_actions for k in ("web", "research"))
                and _reply_is_garbage(final_assistant_reply)
            )
        ):
            try:
                docs = deep_research(line, 5)
                if len(docs) < 3:
                    docs = web_search(line, 8)
                if len(docs) >= 3:
                    text = "\n".join(
                        f"[{i}] {d['title']}: {d['url']} — {d['snippet'][:220]}"
                        for i, d in enumerate(docs[:6], 1)
                    )
                    if "delegate_breakdown" in last_actions:
                        sys_prompt = (
                            "You produced the plan below WITHOUT web research. Rewrite and complete it "
                            "using ONLY these sources, keeping its structure. Cite sources by their [n] "
                            "number where relevant. Reply with ONLY the improved plan. No thinking process."
                        )
                    else:
                        sys_prompt = (
                            "Answer the user's question thoroughly using ONLY these sources. Be accurate, "
                            "cover the important points, and cite sources by their [n] number where relevant. "
                            "Answer directly — no thinking process, no meta-commentary."
                            + _price_note(line)
                        )
                    answer = ""
                    for _attempt in range(2):
                        answer, _u, _m = api_call(
                            env,
                            {
                                "messages": [
                                    {"role": "system", "content": sys_prompt},
                                    {"role": "user", "content": f"Question: {line}\n\nMy previous reply (ignore if empty): {final_assistant_reply[:400]}\n\nSources:\n{text}"},
                                ],
                                "temperature": 0.3,
                                "max_tokens": 600,
                            },
                            json_mode=False,
                        )
                        answer = (answer or "").strip()
                        if _clean_answer(answer) and not answer.lower().startswith("i can't provide"):
                            break
                    if _clean_answer(answer) and not (answer or "").lower().startswith("i can't provide"):
                        srcs = "\n".join(f"{i}. {d['title']} — {d['url']}" for i, d in enumerate(docs[:5], 1))
                        final_assistant_reply = answer + "\n\nSources:\n" + srcs
                    else:
                        final_assistant_reply = _source_fallback(
                            docs, "The summary model glitched — here's what the sources say:"
                        )
                else:
                    LOG.warning("research backstop: only %d docs for %r", len(docs), line[:60])
                    final_assistant_reply = (
                        "⚠ I couldn't pull up search results right now (the search backend seems "
                        "rate-limited). Give it a minute and ask again — or tell me to answer from "
                        "what I know."
                    )
            except Exception:
                pass
    if final_assistant_reply:
        history.append({"role": "assistant", "content": final_assistant_reply})
        history = history[-MAX_HISTORY:]
        append_conversation({"role": "assistant", "content": final_assistant_reply})
    _spawn_distill()
    return final_assistant_reply or "(no reply)", history


def cmd_chat(args: argparse.Namespace, env: dict[str, str]) -> None:
    if not env.get("AI_API_KEY"):
        print(c("✗ AI_API_KEY missing in .env.", BOLD, RED))
        sys.exit(1)
    if getattr(args, "once", None):
        history = load_conversation(14)
        brain_reply(env, args.once.strip(), history)
        return
    print(BANNER)
    print(c("  chat mode — talk naturally. ", GRAY) + c("quit", BOLD) + c(" to exit", GRAY) + "\n")
    history = load_conversation(14)
    if history:
        print(c(f"  (loaded past conversation — {len(history)} messages)", DIM) + "\n")
    chat_hist: list[str] = []
    while True:
        try:
            line = _readline_arrows(c("jorge > ", CYAN), chat_hist)
        except (EOFError, KeyboardInterrupt):
            print()
            return
        line = line.strip()
        if not line:
            continue
        if line in ("quit", "exit", "q"):
            print(c("  see ya, boss!", GRAY))
            return
        chat_hist.append(line)
        if len(chat_hist) > 100:
            chat_hist.pop(0)
        if line in ("help", "?"):
            show_menu()
            continue
        for free_text_cmd in ("web", "remember", "research", "breakdown", "browser"):
            if line.startswith(free_text_cmd + " "):
                rest = line[len(free_text_cmd):].strip()
                if rest:
                    flag = ""
                    if free_text_cmd == "web" and "--ask" in rest:
                        flag = " --ask"
                        rest = rest.replace("--ask", "").strip()
                    line = f'{free_text_cmd} "{rest}"{flag}'
                    break
        try:
            first = shlex.split(line)[0]
        except ValueError:
            first = ""
        if first == "browser" and len(shlex.split(line)) < 2:
            task = _safe_input(c("  what should I do in the browser? ", CYAN))
            if not task:
                continue
            line = f'browser "{task}"'
        if first in ("email", "web", "sort", "organize", "research", "email-draft", "monitor", "breakdown", "remember", "memory", "forget", "browser", "browser-login", "instagram-upload"):
            try:
                dispatch(shlex.split(line), env)
            except SystemExit:
                pass
            except Exception as e:
                print(c(f"  ✗ {e}", RED))
            done = {
                "email": "Email handled.",
                "web": f"Web search done for: {line[len(first):].strip().strip('\"')}",
                "sort": "File sort done.",
                "organize": "Files organized.",
                "research": f"Research done for: {line[len(first):].strip().strip('\"')}",
                "email-draft": "Email draft handled.",
                "monitor": "System monitored.",
                "breakdown": f"Task breakdown for: {line[len(first):].strip().strip('\"')}",
                "remember": "Note remembered.",
                "memory": "Showed stored notes.",
                "forget": "Memory cleared.",
                "browser": f"Browser task done for: {line[len(first):].strip().strip('\"')}",
                "browser-login": f"Login window for: {line[len(first):].strip().strip('\"')}",
                "instagram-upload": f"Upload finished for: {line[len(first):].strip().strip('\"')}",
            }[first]
            append_conversation({"role": "user", "content": line})
            append_conversation({"role": "assistant", "content": done})
            if first in ("email", "web", "sort"):
                save_memory_entry(f"user: {line}")
                save_memory_entry(f"assistant: {done}")
            continue
        reply, history = brain_reply(env, line, history)


def show_menu() -> None:
    print(BANNER)
    header("How can I help?")
    commands = [
        ("chat", "talk naturally — it picks the right tool for you"),
        ("email", "draft an email with AI and send it"),
        ("email-draft", "draft a tone-aware email (review before send)"),
        ("web", "search the web (--ask for an AI answer)"),
        ("research", "deep web research with cited sources"),
        ("sort", "organize files into folders by type"),
        ("organize", "organize files by type AND naming patterns"),
        ("monitor", "system resources, disk, processes + alerts"),
        ("breakdown", "break a task into sub-tasks and delegate"),
        ("browser", "do a task in a real browser (append 'headed' for a visible window)"),
        ("browser-login", "open the browser visibly so YOU log in (captcha/2FA safe)"),
        ("instagram-upload", "upload a video to instagram (no AI brain, reliable)"),
        ("remember", "store a note jorge remembers"),
        ("memory", "show stored notes"),
        ("forget", "clear stored notes"),
    ]
    for name, desc in commands:
        print(c(f"  {name:<17}", BOLD, GREEN) + c(desc, GRAY))
    print(c("\n  e.g. ", CYAN) + c("jorge email --to friend@x.com --subject \"Hi\" --topic \"ask about saturday\"", GRAY))
    print()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Your personal AI assistant: emails, web search, file sorting, memory")
    p.add_argument("--version", action="version", version=f"jorge {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    e = sub.add_parser("email", help="draft and send an email with AI")
    e.add_argument("--to", required=True)
    e.add_argument("--subject", required=True)
    e.add_argument("--topic", required=True)
    e.add_argument("--tone", default="professional")
    e.add_argument("--context", default=None)
    e.set_defaults(func=cmd_email)

    s = sub.add_parser("sort", help="sort files in a folder into categories")
    s.add_argument("path")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_sort)

    w = sub.add_parser("web", help="search the web (DuckDuckGo, free)")
    w.add_argument("query")
    w.add_argument("--ask", action="store_true", help="have AI summarize the results")
    w.set_defaults(func=cmd_web)

    mu = sub.add_parser("music", help="find a Spotify song/playlist link")
    mu.add_argument("query")
    mu.set_defaults(func=cmd_music)

    br = sub.add_parser("browser", help="do a task in a real headless browser (login, forms, downloads)")
    br.add_argument("task")
    br.add_argument("--url", default=None)
    br.set_defaults(func=cmd_browser)

    bl = sub.add_parser("browser-login", help="open the profile VISIBLY so you can log in manually (captcha/2FA safe)")
    bl.add_argument("site")
    bl.set_defaults(func=cmd_browser_login)

    iu = sub.add_parser("instagram-upload", help="upload a video to instagram with a hardcoded flow (no AI brain); append 'headed' for a visible window")
    iu.add_argument("file", nargs="+")
    iu.add_argument("--caption", default="")
    iu.set_defaults(func=cmd_instagram_upload)

    rm = sub.add_parser("remember", help="store a note/topic jorge should remember")
    rm.add_argument("text", nargs="*")
    rm.set_defaults(func=cmd_remember)

    mm = sub.add_parser("memory", help="show stored notes")
    mm.set_defaults(func=cmd_memory)

    c = sub.add_parser("chat", help="talk naturally — it picks the right tool for you")
    c.add_argument("--once", default=None, help="process one message and exit (for voice/avatar use)")
    c.set_defaults(func=cmd_chat)

    fg = sub.add_parser("forget", help="clear all stored notes")
    fg.set_defaults(func=cmd_forget)

    org = sub.add_parser("organize", help="organize files by type AND naming patterns")
    org.add_argument("path")
    org.add_argument("--dry-run", action="store_true")
    org.set_defaults(func=cmd_organize)

    rs = sub.add_parser("research", help="deep web research with cited sources")
    rs.add_argument("query")
    rs.add_argument("-n", "--limit", type=int, default=8, help="number of results")
    rs.add_argument("--ask", action="store_true", help="have AI summarize with citations")
    rs.set_defaults(func=cmd_research)

    em = sub.add_parser("email-draft", help="draft a tone-aware email (review before send)")
    em.add_argument("--to", required=True)
    em.add_argument("--subject", default=None)
    em.add_argument("--topic", required=True)
    em.add_argument("--tone", default="professional")
    em.add_argument("--context", default=None)
    em.add_argument("--recall", action="store_true", help="pull recent conversation context")
    em.add_argument("--no-send", action="store_true", help="only draft, don't send")
    em.set_defaults(func=cmd_email_draft)

    mo = sub.add_parser("monitor", help="system resources, disk, processes with alerts")
    mo.add_argument("--top", type=int, default=5, help="top N processes")
    mo.add_argument("--cpu", type=float, default=90, help="cpu alert threshold %")
    mo.add_argument("--mem", type=float, default=85, help="memory alert threshold %")
    mo.add_argument("--disk", type=float, default=90, help="disk alert threshold %")
    mo.set_defaults(func=cmd_monitor)

    db = sub.add_parser("breakdown", help="break a complex task into sub-tasks and delegate")
    db.add_argument("task")
    db.add_argument("--dir", default=None, help="working folder for delegation")
    db.add_argument("--steps", type=int, default=5, help="max sub-steps")
    db.add_argument("--dry-run", action="store_true", help="only print the breakdown")
    db.set_defaults(func=cmd_delegate_breakdown)
    return p


def dispatch(argv: list[str], env: dict[str, str]) -> None:
    args = build_parser().parse_args(argv)
    if args.command in ("email", "web", "music", "chat", "remember", "memory", "forget", "research", "email-draft", "breakdown", "browser", "browser-login", "instagram-upload"):
        args.func(args, env)
    else:
        args.func(args)


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    env = load_env()
    if len(sys.argv) == 1:
        show_menu()
        cmd_chat(argparse.Namespace(), env)
        return
    dispatch(sys.argv[1:], env)


if __name__ == "__main__":
    main()