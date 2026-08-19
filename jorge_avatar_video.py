#!/usr/bin/env python3
"""Render jorge's avatar animation (idle + blinking + talking) to ~/Videos/jorge_avatar.mp4."""

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path.home() / "Videos" / "jorge_avatar.mp4"
W, H = 640, 640
FPS = 24
DUR = 6.0

BG = (26, 20, 16)
CREAM = (240, 230, 208)
GOLD = (230, 184, 76)

HAT = [
    "      _______      ",
    "     |  ___  |     ",
    "     | |_^_| |     ",
    "     |_______|     ",
    "      \\_____/      ",
]

FACES = {
    "idle":    ["    ( \u2022_\u2022 )     ", "     /| |\\        ", "      / \\         "],
    "blink":   ["    ( -_- )     ", "     /| |\\        ", "      / \\         "],
    "talk1":   ["    ( o_O )     ", "     /| |\\        ", "      / \\         "],
    "talk2":   ["    ( O_o )     ", "     /| |\\        ", "      / \\         "],
    "talk3":   ["    ( o_0 )     ", "     /| |\\        ", "      / \\         "],
}


def render_frame(state: str) -> Image.Image:
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 34)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    lines = HAT + FACES.get(state, FACES["idle"])
    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    total = line_h * len(lines)
    y = (H - total) // 2 - 20
    for line in lines:
        w = d.textlength(line, font=font)
        d.text(((W - w) / 2, y), line, font=font, fill=GOLD if state == "idle" else CREAM)
        y += line_h
    if state != "idle":
        d.text((W / 2, y + 18), "jorge", anchor="mm", font=ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 22), fill=GOLD)
    return img


def main() -> int:
    frames = int(DUR * FPS)
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
         "-r", str(FPS), "-i", "-", "-pix_fmt", "yuv420p", str(OUT)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    assert proc.stdin
    for i in range(frames):
        t = i / FPS
        if t < 2.6:
            blink = abs((t - 0.9) % 1.6) < 0.09 or abs((t - 1.7) % 1.6) < 0.09
            state = "blink" if blink else "idle"
        elif t < 5.4:
            state = f"talk{1 + (i // 3) % 3}"
        else:
            state = "idle"
        frame = render_frame(state).tobytes()
        proc.stdin.write(frame)
    proc.stdin.close()
    rc = proc.wait()
    if rc != 0:
        print(f"ffmpeg failed ({rc})")
        return rc
    print(f"done: {OUT} ({frames} frames, {DUR}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
