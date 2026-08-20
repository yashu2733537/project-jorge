#!/usr/bin/env python3
"""jorge frontend — chat GUI with the same brain (assistant.py), with a voice toggle."""

import queue
import re
import subprocess
import threading
import time
import tkinter as tk

from jorge_body import (
    BG, PANEL, GOLD, CREAM, DIM, HAT, FACES,
    speak_thread, PIPER_MODEL, TTS_WAV,
)

SCRIPT_DIR = __import__("pathlib").Path(__file__).resolve().parent


class JorgeFrontend:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.state = "idle"
        self.speaking = False
        self.talk_frame = 0
        self.tts_queue: queue.Queue[str] = queue.Queue()
        threading.Thread(target=speak_thread, args=(self.tts_queue, self.set_state), daemon=True).start()

        root.title("jorge")
        root.geometry("460x680")
        root.configure(bg=BG)
        root.attributes("-topmost", True)

        topbar = tk.Frame(root, bg=BG)
        topbar.pack(fill="x", padx=14, pady=(10, 0))
        self.mode_label = tk.Label(topbar, text="BUILD", font=("DejaVu Sans Mono", 9, "bold"),
                                   bg=BG, fg="#9fd08a")
        self.mode_label.pack(side="left")
        self.model_label = tk.Label(topbar, text="model: ?", font=("DejaVu Sans Mono", 8),
                                    bg=BG, fg=DIM)
        self.model_label.pack(side="right", padx=(0, 6))
        self.stfu_btn = tk.Button(topbar, text="STFU", font=("DejaVu Sans Mono", 9, "bold"),
                                  bg=PANEL, fg="#e07a6a", activebackground="#5a302a",
                                  relief="flat", padx=8, command=self.stop_speech)
        self.stfu_btn.pack(side="right")

        self.face = tk.Label(root, text="", font=("DejaVu Sans Mono", 15), bg=BG, fg=CREAM, justify="center")
        self.face.pack(pady=(16, 0))

        self.chat = tk.Text(root, height=16, bg=PANEL, fg=CREAM, font=("DejaVu Sans Mono", 9),
                            relief="flat", wrap="word", padx=10, pady=8, state="disabled")
        self.chat.pack(fill="both", expand=True, padx=14, pady=10)
        self.chat.tag_config("you", foreground=GOLD, font=("DejaVu Sans Mono", 9, "bold"))
        self.chat.tag_config("jorge", foreground="#9fd08a", font=("DejaVu Sans Mono", 9, "bold"))
        self.chat.tag_config("body", foreground=CREAM)

        self.status = tk.Label(root, text="idle", font=("DejaVu Sans Mono", 9), bg=BG, fg=GOLD)
        self.status.pack()

        controls = tk.Frame(root, bg=BG)
        controls.pack(fill="x", padx=14, pady=(8, 2))
        self.speak_var = tk.BooleanVar(value=True)
        tk.Checkbutton(controls, text="🔊 speak replies", variable=self.speak_var, bg=BG, fg=CREAM,
                       selectcolor=PANEL, activebackground=BG, activeforeground=GOLD,
                       font=("DejaVu Sans Mono", 9)).pack(side="left")

        self.entry = tk.Entry(root, bg=PANEL, fg=CREAM, font=("DejaVu Sans Mono", 11),
                              insertbackground=GOLD, relief="flat")
        self.entry.pack(fill="x", padx=14, pady=(4, 6))
        self.entry.bind("<Return>", self.send_text)
        self.root.bind("<Escape>", lambda _e: self.stop_speech())
        self.entry.focus_set()

        tk.Button(root, text="bye", font=("DejaVu Sans Mono", 9), bg=PANEL, fg=DIM,
                  relief="flat", command=root.destroy).pack(pady=(0, 8))

        self.root.after(2500, self.blink)
        self.set_face("idle")
        self.root.after(2000, self.poll_mode)

    def poll_mode(self) -> None:
        try:
            import json
            from pathlib import Path
            state = json.loads(Path(SCRIPT_DIR / "plan_state.json").read_text(encoding="utf-8"))
            on = bool(state.get("mode"))
        except Exception:
            on = False
        self.mode_label.config(
            text="PLAN MODE" if on else "BUILD MODE",
            fg=GOLD if on else "#9fd08a",
        )
        try:
            import assistant
            model = getattr(assistant, "LAST_MODEL", None)
            if not model:
                model = (assistant.load_env().get("AI_MODEL") or "?")
            self.model_label.config(text=f"model: {model}")
        except Exception:
            pass
        self.root.after(2000, self.poll_mode)

    def stop_speech(self) -> None:
        try:
            while True:
                self.tts_queue.get_nowait()
        except queue.Empty:
            pass
        subprocess.run(["pkill", "-f", "piper"], capture_output=True)
        subprocess.run(["pkill", "-f", "aplay"], capture_output=True)
        self.speaking = False
        self.set_state("idle")

    def set_face(self, name: str) -> None:
        self.face.config(text="\n".join(HAT + FACES.get(name, FACES["idle"])))

    def set_state(self, name: str) -> None:
        self.root.after(0, lambda: (self.status.config(text=name), self.set_face(name)))

    def blink(self) -> None:
        if not self.speaking:
            self.set_face("blink")
            self.root.after(160, lambda: self.set_face("idle"))
        self.root.after(3200, self.blink)

    def log(self, who: str, text: str) -> None:
        self.chat.config(state="normal")
        self.chat.insert("end", f"{who}: ", (who,))
        self.chat.insert("end", text + "\n\n", ("body",))
        self.chat.config(state="disabled")
        self.chat.see("end")

    def speak(self, text: str) -> None:
        self.speaking = True
        self.set_state("speaking")
        self.tts_queue.put(text)
        est = max(1.5, len(text.split()) * 0.32)
        self.talk_frame = 0
        self.root.after(220, self.animate_talk)
        threading.Thread(target=self._finish_speech, args=(est,), daemon=True).start()

    def animate_talk(self) -> None:
        if not self.speaking:
            return
        self.talk_frame = (self.talk_frame + 1) % 3
        self.set_face(["talk1", "talk2", "talk3"][self.talk_frame])
        self.root.after(220, self.animate_talk)

    def _finish_speech(self, est: float) -> None:
        time.sleep(est + 1.0)
        self.speaking = False
        self.set_state("idle")

    def send_text(self, _event=None) -> None:
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self.log("you", text)
        self.set_state("thinking")
        threading.Thread(target=self._ask, args=(text,), daemon=True).start()

    def _ask(self, text: str) -> None:
        reply = ""
        try:
            import assistant
            prev = assistant.BOT_MODE
            assistant.BOT_MODE = True
            try:
                env = assistant.load_env()
                history = assistant.load_conversation(12)
                reply, _history = assistant.brain_reply(env, text, history)
            finally:
                assistant.BOT_MODE = prev
        except Exception as e:
            reply = f"⚠ {e}"
        self.root.after(0, self._show_reply, reply or "(no reply)")

    def _show_reply(self, reply: str) -> None:
        self.log("jorge", reply)
        if self.speak_var.get():
            self.speak(reply)
        else:
            self.set_state("idle")


def main() -> None:
    root = tk.Tk()
    JorgeFrontend(root)
    root.mainloop()


if __name__ == "__main__":
    main()