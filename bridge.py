#!/usr/bin/env python3
"""WhatsApp bridge: reads one JSON line {user, text} on stdin, writes {reply} to stdout."""
import contextlib
import json
import os
import sys

os.environ["JORGE_BOT"] = "1"
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import assistant as A

BASE = os.path.dirname(os.path.realpath(__file__))


def main():
    line = sys.stdin.readline()
    if not line:
        return
    req = json.loads(line)
    env = A.load_env()
    user = req.get("user", "default")
    import hashlib
    hfile = os.path.join(BASE, "wa_history_" + hashlib.md5(user.encode()).hexdigest()[:6] + ".json")
    history = []
    if os.path.exists(hfile):
        try:
            history = json.load(open(hfile, encoding="utf-8"))
        except Exception:
            history = []
    with contextlib.redirect_stdout(sys.stderr):
        reply, history = A.brain_reply(env, req.get("text", ""), history)
    try:
        json.dump(history[-40:], open(hfile, "w", encoding="utf-8"))
    except Exception:
        pass
    print(json.dumps({"reply": reply or "(no reply)"}), flush=True)


if __name__ == "__main__":
    main()
