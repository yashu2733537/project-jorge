#!/usr/bin/env python3
"""jorge TUI — a beautiful terminal interface for the assistant.

Runs the same brain as `assistant.py chat` (brain_reply) in a worker thread,
so the UI stays buttery while jorge thinks. Replies render as markdown in
speech bubbles; a live sidebar shows system stats, memory notes, and the
current API config.

Run:  python3 assistant.py tui
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import threading
import time
from queue import Empty, Queue

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Footer, Header, Input, Markdown, Static

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, SCRIPT_DIR)

APP_VERSION = "1.0.0"

try:
    import assistant as A
except Exception:  # pragma: no cover
    A = None

MAX_REPLAY = 12


class JorgeTUI(App):
    """jorge — your personal AI assistant, reimagined as a TUI."""

    TITLE = "J O R G E"
    SUB_TITLE = "your personal AI assistant"

    CSS = """
    $background: #0b0f1a;
    $surface: #121826;
    $surface-lighten-1: #1a2334;
    $surface-lighten-2: #233147;
    $primary: #e6b84c;
    $accent: #e6b84c;
    $secondary: #7dd3fc;
    $success: #34d399;
    $warning: #fbbf24;
    $error: #f87171;
    $text: #e8edf6;
    $text-muted: #93a0b8;
    $text-disabled: #5b6880;
    $text-on-accent: #211a07;
    $panel: #141b2b;

    Screen {
        background: $background;
        color: $text;
    }

    #main {
        height: 1fr;
        padding: 0 1;
    }

    /* ---------- chat column ---------- */
    #chatcol {
        height: 1fr;
        min-width: 40;
    }

    #chat {
        height: 1fr;
        overflow-y: auto;
        padding: 1;
        scrollbar-background: $surface;
        scrollbar-color: $accent;
        scrollbar-size-vertical: 1;
    }

    #promptbar {
        height: auto;
        padding: 1 0 0 0;
        border-top: solid $surface-lighten-2;
    }

    #prompt {
        background: $surface-lighten-1;
        border: round $surface-lighten-2;
        color: $text;
    }
    #prompt:focus {
        border: round $accent;
    }

    .row {
        width: 100%;
        height: auto;
        margin: 0 0 1 0;
    }
    .row.user { align: right middle; }
    .row.assistant { align: left middle; }
    .row.sys { align: center middle; }

    .bubble {
        max-width: 84%;
        height: auto;
        padding: 1 2;
        background: $surface-lighten-1;
        border: round $primary;
        color: $text;
    }
    .bubble.user {
        background: $accent;
        border: none;
        color: $text-on-accent;
    }
    .bubble.who {
        background: transparent;
        border: none;
        padding: 0 2;
        color: $text-muted;
        text-style: bold;
    }
    .who.user { color: $accent; }

    Markdown {
        height: auto;
        margin: 0;
        padding: 0;
        background: transparent;
    }

    .sysline {
        color: $text-muted;
        text-style: italic;
        padding: 0 2;
    }

    #typing {
        color: $accent;
        text-style: bold;
        display: none;
    }
    #typing.active { display: block; }

    .hero {
        width: 100%;
        height: auto;
        margin: 0 0 1 0;
        padding: 1 2;
        background: $panel;
        border: round $primary;
        color: $text;
    }
    .hero-title {
        color: $primary;
        text-style: bold;
    }

    /* ---------- sidebar ---------- */
    #sidebar {
        width: 34;
        height: 1fr;
        border-left: solid $surface-lighten-2;
        padding: 0 1 0 2;
    }
    #sidebar .panel-title {
        color: $primary;
        text-style: bold;
        margin: 1 0 0 0;
    }
    #sidebar .panel {
        height: auto;
        background: $panel;
        border: tall $surface-lighten-1;
        padding: 1 2;
        margin: 0 0 1 0;
    }

    #status-line {
        height: 1;
        color: $text-muted;
        margin: 0 0 1 0;
    }

    #stats-detail {
        color: $text-muted;
        padding: 0;
    }

    .hint {
        color: $text-disabled;
        padding: 0;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+l", "clear", "Clear chat"),
        Binding("alt+s", "toggle_sidebar", "Sidebar"),
    ]

    status: reactive[str] = reactive("idle")

    def __init__(self, history: list[dict[str, str]] | None = None) -> None:
        super().__init__()
        self.history: list[dict[str, str]] = list(history or [])
        self._queue: Queue = Queue()
        self._started = time.monotonic()
        self._static_model = "unknown"
        self._dark = True

    # ---------------- lifecycle ----------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="chatcol"):
                with VerticalScroll(id="chat"):
                    yield Static(
                        "◆  J O R G E\n\n"
                        "your personal AI assistant — web search, research with citations, "
                        "email, file sorting, chess, memory, and self-improvement.\n\n"
                        "Just type. Try *research neural nets* or *what's up on the system?*",
                        classes="hero",
                    )
                    yield Static("─", classes="sysline")
                    yield Static("jorge is thinking…", id="typing")
                with Container(id="promptbar"):
                    yield Input(
                        placeholder="Ask jorge anything…  ( /help )",
                        id="prompt",
                    )
            with Vertical(id="sidebar"):
                yield Static("", id="status-line")
                yield Static("SYSTEM", classes="panel-title")
                yield Static("", id="stats", classes="panel")
                yield Static("MEMORY", classes="panel-title")
                yield Static("", id="memory", classes="panel")
                yield Static("COMMANDS", classes="panel-title")
                yield Static(
                    "plain text → talk to jorge\n"
                    "/clear  wipe this chat view\n"
                    "/help   this list\n"
                    "/memory show stored notes\n"
                    "plan mode on / off → approve plans first",
                    id="tips",
                    classes="hint",
                )
        yield Footer()

    def on_mount(self) -> None:
        for msg in self.history[-MAX_REPLAY:]:
            self._render_bubble(msg["role"], msg["content"])
        self.set_interval(0.15, self._drain)
        self.set_interval(5.0, self._refresh_sidebar)
        self.set_interval(0.6, self._blink_typing)
        self._refresh_sidebar()
        self.query_one("#prompt", Input).focus()

    def on_unmount(self) -> None:
        pass

    # ---------------- rendering helpers ----------------

    def _blink_typing(self) -> None:
        typing = self.query_one("#typing", Static)
        if typing.has_class("active"):
            typing.update("jorge is thinking" + ("…" if str(typing.renderable).endswith("…") else ""))

    def _render_bubble(self, role: str, content: str) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        row = Container(classes=f"row {role}")
        who = Static("YOU" if role == "user" else "JORGE", classes=f"bubble who {role}")
        if role == "assistant":
            body = Markdown(str(content), classes="bubble body")
        else:
            body = Static(str(content), classes="bubble user")
        chat.mount(row)
        row.mount(who)
        row.mount(body)
        chat.scroll_end(animate=False)

    def _render_sys(self, text: str) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        chat.mount(Static(str(text), classes="sysline"))
        chat.scroll_end(animate=False)

    # ---------------- sidebar ----------------

    def _refresh_sidebar(self) -> None:
        stats_widget = self.query_one("#stats", Static)
        if A is None:
            stats_widget.update("assistant not importable")
            return

        lines = []
        if A.collect_system_stats:
            try:
                stats = A.collect_system_stats()
                if stats:
                    lines.append(f"CPU      {stats.get('cpu_percent', '?')}%")
                    lines.append(
                        f"MEM      {A.fmt_bytes(stats.get('mem_used', 0))} / "
                        f"{A.fmt_bytes(stats.get('mem_total', 0))} ({stats.get('mem_percent', '?')}%)"
                    )
                    lines.append(
                        f"DISK     {A.fmt_bytes(stats.get('disk_used', 0))} / "
                        f"{A.fmt_bytes(stats.get('disk_total', 0))} ({stats.get('disk_percent', '?')}%)"
                    )
                    lines.append(f"LOAD     {', '.join(map(str, stats.get('loadavg', '?')))}")
            except Exception:
                pass
        self._static_model = A.DEFAULT_MODEL if hasattr(A, "DEFAULT_MODEL") else "unknown"
        uptime = int(time.monotonic() - self._started)
        lines.append(f"UPTIME   {uptime // 3600}h {(uptime % 3600) // 60}m")
        stats_widget.update("\n".join(lines) or "(psutil unavailable)")

        memory_widget = self.query_one("#memory", Static)
        try:
            notes = A.load_memory()
            if notes:
                memory_widget.update("\n".join("• " + str(n)[:70] for n in notes[-6:]))
            else:
                memory_widget.update("nothing yet — tell jorge things to remember")
        except Exception:
            memory_widget.update("(memory unavailable)")

        status_line = self.query_one("#status-line", Static)
        if self.status == "thinking":
            status_line.update("[#fbbf24]●[/] thinking… · " + self._static_model)
        else:
            status_line.update("[#34d399]●[/] online · " + self._static_model)

    # ---------------- input ----------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = (event.value or "").strip()
        event.input.value = ""
        if not text:
            return
        self._handle(text)

    def _handle(self, text: str) -> None:
        first = text.split()[0].lower() if text.split() else ""
        if first.startswith("/"):
            if first == "/help":
                self._render_sys("/clear  /memory  /help   plan mode on|off")
                self._render_bubble("user", text)
                self._render_bubble("assistant", self._help_text())
                return
            if first == "/clear":
                self._clear_chat()
                return
            if first == "/memory":
                self._render_bubble("user", text)
                try:
                    notes = A.load_memory() if A else []
                    body = "\n\n".join(f"• {n}" for n in notes) if notes else "No stored notes yet."
                except Exception as e:
                    body = f"⚠ {e}"
                self._render_bubble("assistant", body)
                return
            if first in ("/quit", "/exit"):
                self.exit()
                return

        self._render_bubble("user", text)
        self.status = "thinking"
        self.query_one("#status-line", Static).update("[#fbbf24]●[/] thinking… · " + self._static_model)
        typing = self.query_one("#typing", Static)
        typing.set_class(True, "active")
        threading.Thread(target=self._think, args=(text,), daemon=True).start()

    def _help_text(self) -> str:
        return (
            "**How to talk to me**\n\n"
            "- Just type naturally — I pick the right tool: research, web, chess, email, file stuff, memory…\n"
            "- `plan mode on` → I propose first and wait for your OK before doing anything\n"
            "- `research <topic>` → deep, cited research\n"
            "- `chess vs 1500` or `start a chess game` → play me\n\n"
            "**Keys:** `ctrl+q` quit · `ctrl+l` clear · `alt+s` sidebar\n"
            "**Sidebar:** live CPU/mem/disk, stored memory, quick commands"
        )

    # ---------------- brain worker ----------------

    def _think(self, text: str) -> None:
        try:
            env = A.load_env()
            if not env.get("AI_API_KEY"):
                self._queue.put({"type": "reply", "text": "⚠ AI_API_KEY missing — set it in .env", "error": True})
                return
            out, err = io.StringIO(), io.StringIO()
            old_stdin = sys.stdin
            sys.stdin = io.StringIO()
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    t0 = time.monotonic()
                    reply, new_history = A.brain_reply(env, text, list(self.history), user="tui")
                    elapsed = time.monotonic() - t0
                self.history = new_history
            finally:
                sys.stdin = old_stdin
            for line in out.getvalue().splitlines():
                s = line.strip()
                if not s or s.startswith("  > "):
                    continue
                if "tokens" in s or "\r" in s:
                    continue
                self._queue.put({"type": "progress", "text": s[:220]})
            self._queue.put({"type": "reply", "text": reply, "elapsed": elapsed, "error": False})
            if err.getvalue():
                self._queue.put({"type": "progress", "text": "stderr: " + err.getvalue()[:200]})
        except Exception as e:
            self._queue.put({"type": "reply", "text": "⚠ " + str(e), "error": True})

    def _drain(self) -> None:
        got_any = False
        while True:
            try:
                item = self._queue.get_nowait()
            except Empty:
                break
            got_any = True
            if item.get("type") == "progress":
                self._render_sys(item.get("text", ""))
            elif item.get("type") == "reply":
                self.status = "idle"
                typing = self.query_one("#typing", Static)
                typing.set_class(False, "active")
                if item.get("error"):
                    self._render_bubble("assistant", item["text"])
                else:
                    text = item["text"]
                    elapsed = item.get("elapsed", 0)
                    if elapsed:
                        text = text + f"\n\n_⚡ {elapsed:.1f}s_"
                    self._render_bubble("assistant", text)
                self._refresh_sidebar()
        if not got_any:
            pass

    # ---------------- actions ----------------

    def action_clear(self) -> None:
        self._clear_chat()

    def _clear_chat(self) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        chat.remove_children()
        self._render_bubble("assistant", "Chat cleared — fresh brain, same memory.")

    def action_focus_input(self) -> None:
        self.query_one("#prompt", Input).focus()

    def action_toggle_sidebar(self) -> None:
        sidebar = self.query_one("#sidebar", Vertical)
        sidebar.display = not sidebar.display
        self.query_one("#prompt", Input).focus()


def main() -> int:
    if A is None:
        print("✗ could not import assistant — run me from the project root.")
        return 1
    history = A.load_conversation(14)
    app = JorgeTUI(history=history)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())