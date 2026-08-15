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
from pathlib import Path
from typing import Any

import requests

__version__ = "2.0.0"

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
MODEL_QUARANTINE_H = 3

BOT_MODE = os.environ.get("JORGE_BOT") == "1"

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


def save_memory_entry(text: str) -> None:
    try:
        with MEMORY_FILE.open("a", encoding="utf-8") as f:
            f.write(text.strip() + "\n")
    except OSError as e:
        print(c("✗ could not write memory:", BOLD, RED), e)
        return
    print(c("✓ Remembered.", BOLD, GREEN), c(f"({len(load_memory())} notes total)", GRAY))


def forget_memory() -> None:
    if MEMORY_FILE.exists():
        MEMORY_FILE.unlink()
    print(c("✓ Memory cleared.", BOLD, GREEN))


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

def draft_email(env: dict[str, str], to: str, subject: str, topic: str, tone: str = "professional",
                context: str | None = None) -> tuple[str, dict]:
    system = (
        "You are a helpful email-writing assistant. Write a clear, concise email "
        f"with a {tone} tone. Return ONLY the email body (text after the subject). "
        "No preamble, no signature, no 'Subject:' line."
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
    body, usage, _model = api_call(env, payload, json_mode=False)
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


def send_email(env: dict[str, str], to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{env.get('SMTP_NAME', env['SMTP_USER'])} <{env['SMTP_USER']}>"
    msg["To"] = to
    msg.set_content(body)
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


def email_flow(env: dict[str, str], to: str, subject: str, topic: str, tone: str = "professional") -> str:
    body = _email_approve_loop(env, to, subject, topic, tone)
    if body is None:
        return "cancelled"
    print(c("  ✈ sending...", CYAN))
    send_email(env, to, subject, body)
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
    for fetcher in (_ddg_lite, _ddg_html):
        try:
            results = fetcher(query, n)
            if results:
                return results
        except (requests.RequestException, ValueError):
            LOG.debug("search backend %s failed", fetcher.__name__)
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


# ---------------- MEMORY COMMANDS ----------------

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


# ---------------- CHAT / MODEL ----------------

CHAT_SYSTEM = """You are jorge, a friendly personal assistant in a terminal with FULL access to the user's computer.
Facts: the user's HOME folder is {HOME}, username is {USER}, OS is Linux.
The user talks to you in plain language.
You must reply with ONLY one JSON object, no markdown fences, no extra text, no reasoning, no comments. Never output anything outside the JSON object. Pick exactly one action:

{"action": "chat", "reply": "your answer"}
{"action": "web", "query": "search query"}
{"action": "email", "to": "recipient@example.com", "subject": "...", "topic": "what the email should say"}
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

Rules:
- If the user asks to email/send a message -> action email. Extract recipient, subject, and the content/topic. Use "" if unknown.
- If the user asks to search/check something online -> action web.
- If the user asks to organize/sort files -> action sort.
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
        skip = {m: t for m, t in state.get("skip", {}).items() if now - t < MODEL_QUARANTINE_H * 3600}
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
                        state.setdefault("skip", {}).pop(model, None)
                        _save_model_state(state)
                        return (
                            data["choices"][0]["message"]["content"].strip(),
                            data.get("usage", {}),
                            data.get("model", env.get("AI_MODEL", DEFAULT_MODEL)),
                        )
                    last_err = "API returned no reply"
                    continue
                if r.status_code == 429:
                    state.setdefault("skip", {})[model] = now
                    _save_model_state(state)
                    last_err = "API error 429 (quota)"
                    break
                if r.status_code in (401, 403):
                    raise RuntimeError(f"API error {r.status_code}: {r.text[:200]}")
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


def run_shell(command: str, timeout: int = 120) -> str:
    r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
    out = (r.stdout or "") + (r.stderr or "")
    if len(out) > MAX_TOOL_REPLY:
        out = out[:MAX_TOOL_REPLY] + "\n...[truncated]"
    return out.strip() or "(no output, exit code " + str(r.returncode) + ")"


def run_delegate(task: str, dir_arg: str) -> str:
    scrub = re.sub(r"don'?t\s+touch[^.]*?(?:godot|game)[^.]*\.", "", task, flags=re.I)
    blocked = [w for w in ("my-game-my-legacy-2", ".tscn", "win_screen", "boss fight", "boss_fight", "godot") if w in scrub.lower()]
    if dir_arg and "my-game-my-legacy-2" in dir_arg:
        blocked.append("game dir")
    if blocked:
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
    cmd = [binary, "run", task + "\n\nAfter finishing, reply with a short plain-text summary (2-3 sentences, no code) of what you changed and how to use it, prefixed with SUMMARY:"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(workdir), env=env2, timeout=900)
    except subprocess.TimeoutExpired:
        return "opencode timed out after 15 minutes."
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 and not out.strip():
        return f"opencode failed (exit {r.returncode})."
    return out.strip()[:MAX_TOOL_REPLY] or "(opencode returned nothing)"


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
            return "\n".join(keep)[:600]
    junk = re.compile(r"^(?:\+{1,}|-{1,}|@@|diff |index |---|\+\+\+|◆|\$|→|> )")
    clean = [l.strip() for l in text.split("\n") if l.strip() and not junk.match(l)]
    if clean:
        joined = "\n".join(clean)
        if len(joined) <= 600:
            return joined
        return "Done. " + "\n".join(clean[-3:])[:600]
    return text[:600]


def run_tool(env: dict[str, str], tool: dict[str, Any], history: list[dict[str, str]]) -> tuple[str, bool]:
    action = tool.get("action")
    if action == "chat":
        return tool.get("reply", "").strip(), True
    if action == "web":
        query = tool.get("query", "").strip()
        if not query:
            query = ask_bot("what should I search for")
        results = web_search(query, 5)
        if not results:
            return "No results found.", True
        for i, res in enumerate(results, 1):
            print(c(f"  {i:>2}. ", CYAN) + c(res["title"], BOLD))
            print(c("      " + res["url"], DIM))
        answer, usage, model = chat_call(env, [
            {"role": "system", "content": "Summarize the search results to answer the question concisely."},
            {"role": "user", "content": f"Question: {query}\n\nResults:\n" + "\n".join(f"- {r['title']}: {r['snippet']}" for r in results)},
        ], max_tokens=300)
        print(c("  ── ANSWER ──", CYAN))
        box(answer, GREEN)
        print(tokens_str(usage, model))
        return "", True
    if action == "email":
        to = tool.get("to", "").strip()
        subject = tool.get("subject", "").strip()
        topic = tool.get("topic", "").strip()
        if not to:
            to = ask_bot("who is it to")
        if not subject:
            subject = ask_bot("subject")
        if not topic:
            topic = ask_bot("what should it say")
        if not to or not subject or not topic:
            return "Email cancelled — missing details.", True
        if email_flow(env, to, subject, topic) == "sent":
            return "Email sent successfully.", True
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
        command = tool.get("command", "").strip()
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
            if not BOT_MODE:
                return text, True
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
    system += (
        "\n\nYOUR OWN CODE (self-knowledge index — always up to date with your current source, "
        "refreshed whenever your code changes). Use it to answer questions about yourself and "
        "to make precise self edits:\n" + get_self_index()
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


def brain_reply(env: dict[str, str], line: str, history: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    line = _answer_context(line, history)
    line = _selection_context(line, history)
    history.append({"role": "user", "content": line})
    history = history[-MAX_HISTORY:]
    append_conversation({"role": "user", "content": line})
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
                            msgs + [{"role": "user", "content": "You got interrupted mid-task. Tell the user in 1-2 short plain sentences what you have done so far and what you were about to do next. No JSON, just a quick status update."}],
                            max_tokens=250,
                        )
                        final_assistant_reply = wrap.strip()[:600] or "I got interrupted mid-task — what should I do next?"
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
                    print(c("  > ", CYAN) + (completion_summary or "Done."))
                    final_assistant_reply = completion_summary or "Done."
                    break
                msgs.append({"role": "user", "content": 'You already ran that action and it succeeded. The task is done — reply with ONLY {"action":"chat","reply":"<a short completion message to the user>"} confirming what you did. Do not run any more actions.'})
                continue
            last_actions.append(key)
            try:
                reply, done = run_tool(env, tool, history)
            except MissingInfo as mi:
                final_assistant_reply = f"I need more info: {mi}."
                break
            except Exception as e:
                final_assistant_reply = f"⚠ {e}"
                break
            completion_summary = summarize_tool(tool, reply, completion_summary)
            if done:
                show = reply.strip()
                if show and tool.get("action") == "shell" and len(show.splitlines()) > 3:
                    try:
                        wrap, usage, model = chat_call(
                            env,
                            msgs + [{"role": "user", "content": f"[A command you ran just finished. Command:\n{tool.get('command','')}\n\nOutput:\n{show[:1500]}\n\nTell the user the result in 1-2 short plain sentences with the important facts (like how many files were deleted or moved). Do NOT paste the raw output.]"}],
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
                    if wrap.strip():
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
    if final_assistant_reply:
        history.append({"role": "assistant", "content": final_assistant_reply})
        history = history[-MAX_HISTORY:]
        append_conversation({"role": "assistant", "content": final_assistant_reply})
    return final_assistant_reply or "(no reply)", history


def cmd_chat(args: argparse.Namespace, env: dict[str, str]) -> None:
    if not env.get("AI_API_KEY"):
        print(c("✗ AI_API_KEY missing in .env.", BOLD, RED))
        sys.exit(1)
    print(BANNER)
    print(c("  chat mode — talk naturally. ", GRAY) + c("quit", BOLD) + c(" to exit", GRAY) + "\n")
    history = load_conversation(14)
    if history:
        print(c(f"  (loaded past conversation — {len(history)} messages)", DIM) + "\n")
    while True:
        try:
            line = input(c("jorge > ", CYAN))
        except (EOFError, KeyboardInterrupt):
            print()
            return
        line = line.strip()
        if not line:
            continue
        if line in ("quit", "exit", "q"):
            print(c("  see ya, boss!", GRAY))
            return
        if line in ("help", "?"):
            show_menu()
            continue
        for free_text_cmd in ("web", "remember"):
            if line.startswith(free_text_cmd + " "):
                rest = line[len(free_text_cmd):].strip()
                if rest:
                    flag = ""
                    if free_text_cmd == "web" and "--ask" in rest:
                        flag = " --ask"
                        rest = rest.replace("--ask", "").strip()
                    line = f'{free_text_cmd} "{rest}"{flag}'
                    break
        first = shlex.split(line)[0]
        if first in ("email", "web", "sort", "remember", "memory", "forget"):
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
                "remember": "Note remembered.",
                "memory": "Showed stored notes.",
                "forget": "Memory cleared.",
            }[first]
            append_conversation({"role": "user", "content": line})
            append_conversation({"role": "assistant", "content": done})
            if first in ("email", "web", "sort"):
                save_memory_entry(f"user: {line}")
                save_memory_entry(f"assistant: {done}")
            continue
        reply, history = brain_reply(env, line, history)
        save_memory_entry(f"user: {line}")
        if reply and reply not in ("(no reply)",):
            save_memory_entry(f"assistant: {reply}")


def show_menu() -> None:
    print(BANNER)
    header("How can I help?")
    commands = [
        ("chat", "talk naturally — it picks the right tool for you"),
        ("email", "draft an email with AI and send it"),
        ("web", "search the web (--ask for an AI answer)"),
        ("sort", "organize files into folders"),
        ("remember", "store a note jorge remembers"),
        ("memory", "show stored notes"),
        ("forget", "clear stored notes"),
    ]
    for name, desc in commands:
        print(c(f"  {name:<10}", BOLD, GREEN) + c(desc, GRAY))
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

    rm = sub.add_parser("remember", help="store a note/topic jorge should remember")
    rm.add_argument("text", nargs="?", default="")
    rm.set_defaults(func=cmd_remember)

    mm = sub.add_parser("memory", help="show stored notes")
    mm.set_defaults(func=cmd_memory)

    c = sub.add_parser("chat", help="talk naturally — it picks the right tool for you")
    c.set_defaults(func=cmd_chat)

    fg = sub.add_parser("forget", help="clear all stored notes")
    fg.set_defaults(func=cmd_forget)
    return p


def dispatch(argv: list[str], env: dict[str, str]) -> None:
    args = build_parser().parse_args(argv)
    if args.command in ("email", "web", "chat", "remember", "memory", "forget"):
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