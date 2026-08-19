#!/usr/bin/env python3
"""jorge's body — desktop avatar with a mouth (piper TTS) and ears (vosk STT)."""

import json
import os
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONV_FILE = SCRIPT_DIR / "conversations.jsonl"
PIPER_MODEL = Path.home() / ".local/share/piper/en_US-lessac-medium.onnx"
VOSK_MODEL = Path.home() / ".local/share/vosk/vosk-model-small-en-us-0.15"
TTS_WAV = "/tmp/jorge_say.wav"
MIC_WAV = "/tmp/jorge_mic.wav"

BG = "#1a1410"
PANEL = "#2a1f16"
GOLD = "#e6b84c"
CREAM = "#f0e6d0"
DIM = "#8a7a5f"

HAT = [
    "      _______      ",
    "     |  ___  |     ",
    "     | |_^_| |     ",
    "     |_______|     ",
    "      \\_____/      ",
]

FACES = {
    "idle":    ["    ( •_• )     ", "     /| |\\        ", "      / \\         "],
    "blink":   ["    ( -_- )     ", "     /| |\\        ", "      / \\         "],
    "think":   ["    ( ◔_◔ )     ", "     /| |\\        ", "      / \\         "],
    "talk1":   ["    ( o_O )     ", "     /| |\\        ", "      / \\         "],
    "talk2":   ["    ( O_o )     ", "     /| |\\        ", "      / \\         "],
    "talk3":   ["    ( o_0 )     ", "     /| |\\        ", "      / \\         "],
}


def speak_thread(tts_queue: queue.Queue, set_state) -> None:
    while True:
        text = tts_queue.get()
        if text is None:
            return
        clean = re.sub(r"[✓⚠✗→✦❤⭐�—“”‘’]", " ", text)
        clean = re.sub(r"\s+", " ", clean).strip()
        if not clean:
            continue
        try:
            subprocess.run(
                ["python3", "-m", "piper", "--model", str(PIPER_MODEL), "--output_file", TTS_WAV],
                input=clean.encode(), capture_output=True, timeout=90,
            )
            subprocess.run(["aplay", "-q", TTS_WAV], capture_output=True, timeout=90)
        except Exception:
            pass


class JorgeBody:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.offset = 0
        self.state = "idle"
        self.speaking = False
        self.recording = False
        self.talk_frame = 0
        self.vosk = None
        self.tts_queue: queue.Queue[str] = queue.Queue()
        threading.Thread(target=speak_thread, args=(self.tts_queue, self.set_state), daemon=True).start()

        root.title("jorge")
        root.geometry("360x520")
        root.configure(bg=BG)
        root.attributes("-topmost", True)

        self.face = tk.Label(root, text="", font=("DejaVu Sans Mono", 15), bg=BG, fg=CREAM, justify="center")
        self.face.pack(pady=(18, 0))

        self.bubble = tk.Text(root, height=7, bg=PANEL, fg=CREAM, font=("DejaVu Sans Mono", 9),
                              relief="flat", wrap="word", padx=10, pady=8, state="disabled")
        self.bubble.pack(fill="x", padx=14, pady=10)

        self.status = tk.Label(root, text="idle", font=("DejaVu Sans Mono", 9), bg=BG, fg=GOLD)
        self.status.pack()

        self.talk_btn = tk.Button(root, text="HOLD TO TALK", font=("DejaVu Sans Mono", 11, "bold"),
                                  bg=GOLD, fg="#1a1410", activebackground="#f7d37a",
                                  relief="flat", padx=6, pady=6)
        self.talk_btn.pack(fill="x", padx=14, pady=(12, 6))
        self.talk_btn.bind("<ButtonPress-1>", self.start_rec)
        self.talk_btn.bind("<ButtonRelease-1>", self.stop_rec)

        self.entry = tk.Entry(root, bg=PANEL, fg=CREAM, font=("DejaVu Sans Mono", 10),
                              insertbackground=GOLD, relief="flat")
        self.entry.pack(fill="x", padx=14, pady=(0, 6))
        self.entry.bind("<Return>", self.send_text)

        tk.Button(root, text="bye", font=("DejaVu Sans Mono", 9), bg=PANEL, fg=DIM,
                  relief="flat", command=root.destroy).pack(pady=(2, 10))

        if CONV_FILE.exists():
            self.offset = CONV_FILE.stat().st_size
        self.root.after(600, self.poll)
        self.root.after(2500, self.blink)
        self.set_face("idle")

    def set_face(self, name: str) -> None:
        lines = HAT + FACES.get(name, FACES["idle"])
        self.face.config(text="\n".join(lines))

    def set_state(self, name: str) -> None:
        self.root.after(0, lambda: (self.status.config(text=name), self.set_face(name)))

    def blink(self) -> None:
        if not self.speaking:
            self.set_face("blink")
            self.root.after(160, lambda: self.set_face("idle"))
        self.root.after(3200, self.blink)

    def bubble_add(self, who: str, text: str) -> None:
        self.bubble.config(state="normal")
        self.bubble.insert("end", f"{who}: ", ("who",))
        self.bubble.insert("end", text + "\n\n")
        self.bubble.tag_config("who", foreground=GOLD, font=("DejaVu Sans Mono", 9, "bold"))
        self.bubble.config(state="disabled")
        self.bubble.see("end")

    def poll(self) -> None:
        try:
            size = CONV_FILE.stat().st_size
            if size > self.offset:
                with open(CONV_FILE, "rb") as fh:
                    fh.seek(self.offset)
                    data = fh.read(size - self.offset)
                self.offset = size
                for raw in data.decode("utf-8", "replace").splitlines():
                    if not raw.strip():
                        continue
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    role = msg.get("role", "")
                    content = str(msg.get("content", ""))[:600]
                    if not content:
                        continue
                    self.bubble_add("you" if role == "user" else "jorge", content)
                    if role == "assistant":
                        self.speak(content)
        except FileNotFoundError:
            pass
        self.root.after(600, self.poll)

    def speak(self, text: str) -> None:
        self.speaking = True
        self.set_state("speaking")
        self.tts_queue.put(text)
        words = len(text.split())
        est = max(1.5, words * 0.32)
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

    def start_rec(self, _event=None) -> None:
        if self.recording:
            return
        self.recording = True
        self.set_state("listening")
        self.talk_btn.config(text="RELEASE WHEN DONE")
        subprocess.Popen(
            ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "wav", MIC_WAV],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def stop_rec(self, _event=None) -> None:
        if not self.recording:
            return
        self.recording = False
        self.talk_btn.config(text="HOLD TO TALK")
        self.set_state("thinking")
        threading.Thread(target=self.transcribe_and_ask, daemon=True).start()

    def transcribe_and_ask(self) -> None:
        subprocess.run(["pkill", "-f", "arecord.*jorge_mic"], capture_output=True)
        time.sleep(0.4)
        text = self.transcribe()
        if not text:
            self.set_state("idle")
            return
        self.bubble_add("you", text)
        self.run_jorge(text)

    def transcribe(self) -> str:
        try:
            import wave
            from vosk import KaldiRecognizer, Model
            if self.vosk is None:
                self.vosk = Model(str(VOSK_MODEL))
            with wave.open(MIC_WAV, "rb") as w:
                rec = KaldiRecognizer(self.vosk, w.getframerate())
                rec.AcceptWaveform(w.readframes(w.getnframes()))
                return json.loads(rec.FinalResult()).get("text", "").strip()
        except Exception:
            return ""

    def run_jorge(self, text: str) -> None:
        try:
            subprocess.run(
                ["python3", "assistant.py", "chat", "--once", text],
                cwd=SCRIPT_DIR, capture_output=True, timeout=300,
            )
        except Exception:
            pass

    def send_text(self, _event=None) -> None:
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self.bubble_add("you", text)
        self.set_state("thinking")
        threading.Thread(target=self.run_jorge, args=(text,), daemon=True).start()


def main() -> None:
    root = tk.Tk()
    JorgeBody(root)
    root.mainloop()


if __name__ == "__main__":
    main()