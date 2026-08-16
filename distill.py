#!/usr/bin/env python3
"""Honcho-style memory distillation: extracts durable facts about the user
from recent conversations and stores them in memory.txt, so jorge gets
smarter with every use. Spawned detached after each chat turn."""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import assistant as A

STATE_FILE = A.SCRIPT_DIR / "distill_state.json"
DISTILL_MODEL = "nemotron-3-ultra-free"


def _try_json(raw: str):
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    return json.loads(raw)


def main() -> None:
    try:
        env = A.load_env()
        if not env.get("AI_API_KEY"):
            return
        state: dict = {"offset": 0}
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        try:
            lines = A.CONV_LOG.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        if len(lines) <= state.get("offset", 0):
            return
        pairs = []
        for line in lines[state["offset"]:][-12:]:
            try:
                d = json.loads(line)
                pairs.append({"role": d.get("role", "user"), "content": str(d.get("content", ""))[:1500]})
            except ValueError:
                continue
        if not pairs:
            return
        try:
            env = A.load_env()
            env["AI_MODEL"] = DISTILL_MODEL
            data = None
            for _attempt in range(3):
                try:
                    raw, _usage, _model = A.chat_call(
                        env,
                        [
                            {"role": "system", "content": (
                                "You are the memory layer of a personal AI assistant. From the conversation "
                                "below, extract up to 5 short DURABLE facts about the user that will help in "
                                "future conversations (preferences, identity details, habits, recurring topics) "
                                "AND up to 3 LESSONS: mistakes the assistant made and how to avoid them "
                                "(wrong assumptions, misunderstood requests, failed actions, corrections). "
                                "Skip anything trivial or one-time. Each fact/lesson: one string, max 20 words, "
                                "plain text, no quotes. Reply with ONLY a JSON object "
                                '{"facts": [...], "lessons": [...]} (empty arrays if nothing).'
                            )},
                            {"role": "user", "content": json.dumps(pairs[:8])},
                        ],
                        max_tokens=250,
                    )
                    data = _try_json(raw)
                    if isinstance(data, (list, dict)):
                        break
                    data = None
                except Exception:
                    data = None
            if data is None:
                return
            if isinstance(data, list):
                facts, lessons = data, []
            else:
                facts = data.get("facts", []) if isinstance(data.get("facts"), list) else []
                lessons = data.get("lessons", []) if isinstance(data.get("lessons"), list) else []
            for fact in facts:
                if isinstance(fact, str) and fact.strip():
                    A.save_memory_entry(fact.strip(), replace_similar=True)
            for lesson in lessons:
                if isinstance(lesson, str) and lesson.strip():
                    A.save_mistake(lesson.strip())
        except Exception as e:
            print("distill:", e, file=sys.stderr)
        finally:
            state["offset"] = len(lines)
            try:
                STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
            except OSError:
                pass
    finally:
        try:
            A.DISTILL_LOCK.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()