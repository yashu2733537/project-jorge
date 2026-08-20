#!/usr/bin/env python3
"""Bridge: reads one JSON line {user, text, prefix?, action?, query?} on stdin, writes {reply} to stdout.

- normal chat: brain_reply decides what to do
- with "action" (e.g. research/brainstorm): that tool runs directly, no model routing
- "prefix" namespaces per-user history files (wa_ / dc_ / ...)
"""
import contextlib
import hashlib
import json
import os
import sys

os.environ["JORGE_BOT"] = "1"
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import assistant as A

BASE = os.path.dirname(os.path.realpath(__file__))

LOCKED_ACTIONS = ("shell", "write", "forget", "sort", "organize", "delegate", "email", "email_draft")


def main():
    line = sys.stdin.readline()
    if not line:
        return
    req = json.loads(line)
    env = A.load_env()
    owner_ok = bool(req.get("owner_ok"))
    user = req.get("user", "default")
    prefix = str(req.get("prefix") or "wa")
    hfile = os.path.join(BASE, f"{prefix}_history_" + hashlib.md5(user.encode()).hexdigest()[:6] + ".json")
    history = []
    if os.path.exists(hfile):
        try:
            history = json.load(open(hfile, encoding="utf-8"))
        except Exception:
            history = []
    if not owner_ok:
        orig_run_tool = A.run_tool

        def guarded(env_, tool, history_):
            if tool.get("action") in LOCKED_ACTIONS:
                return f"🔒 '{tool.get('action')}' is locked for non-owners.", True
            return orig_run_tool(env_, tool, history_)

        A.run_tool = guarded
    with contextlib.redirect_stdout(sys.stderr):
        action = req.get("action")
        if action:
            if not owner_ok and action in LOCKED_ACTIONS:
                print(json.dumps({"reply": f"🔒 '{action}' is locked for non-owners."}), flush=True)
                return
            tool = {
                "action": action,
                "query": req.get("query") or req.get("topic") or req.get("text") or "",
                "user": user,
            }
            reply, _done = A.run_tool(env, tool, history)
        else:
            reply, history = A.brain_reply(env, req.get("text", ""), history, user)
    try:
        json.dump(history[-40:], open(hfile, "w", encoding="utf-8"))
    except Exception:
        pass
    print(json.dumps({"reply": reply or "(no reply)"}), flush=True)


if __name__ == "__main__":
    main()