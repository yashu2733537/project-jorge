"""Background worker: polls the SQLite queue, executes workflows, checkpoints.

Design:
  * claim_next() atomically picks the highest-priority queued task and flips it
    to 'running' (SQLite transaction), so multiple workers never run one task.
  * Each step renders its params against the execution state, runs it, then
    writes the outcome (params, output, error, attempts) back to SQLite — both in
    the per-step row and in the task-level checkpoint blob. On a crash the next
    claim resumes from the first non-succeeded step.
  * Failure handling per step: inline retries (retries/retry_delay), then the
    on_error policy (fail | skip | continue). A task-level failure bumps
    task.attempts; if attempts <= max_retries the task is requeued, otherwise it
    is marked failed.
  * Notifications fire on start / success / error / retry per the workflow's
    notify config.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any

from . import store
from .notify import notify_event
from .schema import eval_bool, normalize_definition, render, render_params, safe_eval
from .steps import run_step

LOG = logging.getLogger("pursuit")

DEFAULT_POLL = float(os.environ.get("PURSUIT_POLL_INTERVAL", "5"))

_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PID_FILE = os.path.join(_SCRIPT_DIR, "pursuit_worker.pid")
LOG_FILE = os.path.join(_SCRIPT_DIR, "pursuit_worker.log")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clip(text: str, limit: int = 2000) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n…[truncated]"


def _initial_state(task: dict[str, Any], defn: dict[str, Any]) -> dict[str, Any]:
    return {
        "task": {"id": task["id"], "name": task["name"], "priority": task["priority"]},
        "vars": dict(task.get("vars") or {}),
        "steps": {},
    }


def _load_defn(task: dict[str, Any]) -> dict[str, Any]:
    defn = task.get("payload") or {}
    return normalize_definition(defn)


def _step_summary(step: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    key = step["id"]
    cur = state["steps"].get(key, {})
    return {
        "id": key,
        "tool": step.get("tool"),
        "status": cur.get("status", "pending"),
        "attempts": cur.get("attempts", 0),
        "error": cur.get("error"),
    }


def _task_is_cancelled(task_id: int, db_path: str | None) -> bool:
    task = store.get_task(task_id, db_path=db_path)
    return bool(task and task["status"] == "cancelled")


def execute_task(env: dict[str, str], task: dict[str, Any], db_path: str | None = None) -> str:
    """Run one task to completion (or failure). Returns final task status."""
    task_id = task["id"]
    defn = _load_defn(task)
    state = task.get("checkpoint") or _initial_state(task, defn)
    steps = defn["steps"]

    for idx, step in enumerate(steps):
        key = step["id"]
        cur = state["steps"].get(key, {})
        if cur.get("status") in ("succeeded", "skipped"):
            continue
        if _task_is_cancelled(task_id, db_path):
            _mark_skipped(task_id, key, idx, state, db_path, reason="task cancelled")
            continue

        try:
            if not eval_bool(render(step.get("if"), state), state):
                _mark_skipped(task_id, key, idx, state, db_path, reason="condition not met")
                continue
        except ValueError as e:
            _mark_step(task_id, key, idx, state, db_path, status="failed",
                       error=f"bad condition: {e}", params=step.get("params", {}))
            return _finalize_failure(task, db_path, f"step {key}: {e}")

        params = render_params(step.get("params", {}), state)
        if not isinstance(params, dict):
            params = {}
        params.setdefault("timeout", step.get("timeout") or defn.get("timeout") or 300)

        retries = int(step.get("retries", defn.get("retries", 0)))
        retry_delay = float(step.get("retry_delay", 5))
        result = None
        attempts = int(cur.get("attempts", 0))

        for attempt in range(1, retries + 2):
            attempts = attempts + 1
            _mark_step(task_id, key, idx, state, db_path, status="running",
                       params=params, attempts=attempts)
            result = run_step(env, step, params)
            if result.ok:
                break
            if attempt <= retries:
                LOG.warning("task %s step %s failed (attempt %s/%s): %s — retrying in %ss",
                            task_id, key, attempt, retries + 1, result.error, retry_delay)
                time.sleep(retry_delay)

        if result.ok:
            state["steps"][key] = {
                "status": "succeeded", "attempts": attempts, "params": params,
                "output": result.output, "error": "", "started_at": _now(), "finished_at": _now(),
            }
            if step["tool"] in ("set", "expr"):
                name = str(params.get("name") or "")
                if name:
                    if step["tool"] == "expr":
                        try:
                            value = safe_eval(params.get("value"), state)
                        except ValueError:
                            value = result.output
                    else:
                        value = params.get("value")
                    state["vars"][name] = value
            _save_checkpoint(task_id, key, idx, state, db_path)
            LOG.info("task %s step %s succeeded (attempt %s)", task_id, key, attempts)
            continue

        step_error = f"step {key} failed: {result.error}"
        _mark_step(task_id, key, idx, state, db_path, status="failed",
                   params=params, attempts=attempts, error=step_error)
        on_error = step.get("on_error", defn.get("on_error", "fail"))
        if on_error == "skip":
            _mark_skipped(task_id, key, idx, state, db_path, reason=f"failed after {attempts} attempt(s): {result.error}")
            LOG.warning("task %s step %s failed and was skipped: %s", task_id, key, result.error)
            continue
        if on_error == "continue":
            state["steps"][key]["status"] = "failed"
            _save_checkpoint(task_id, key, idx, state, db_path)
            LOG.warning("task %s step %s failed but continuing: %s", task_id, key, result.error)
            continue
        return _finalize_failure(task, db_path, step_error)

    state["steps"] = {k: v for k, v in state["steps"].items()}
    _save_checkpoint(task_id, None, None, state, db_path, done=True)
    summary = "\n".join(
        f"- {k}: {v.get('status')}" for k, v in state["steps"].items()
    )
    task["checkpoint"] = state
    store.update_task(task_id, db_path=db_path, status="succeeded",
                      finished_at=_now(), checkpoint=state, result=summary)
    notify_event(env, "success", _title(task, "succeeded"), _message(task, summary), task, db_path)
    LOG.info("task %s succeeded", task_id)
    return "succeeded"


def _finalize_failure(task: dict[str, Any], db_path: str | None, error: str) -> str:
    task_id = task["id"]
    task["checkpoint"] = task.get("checkpoint")
    attempts = int(task.get("attempts", 0))
    max_retries = int(task.get("max_retries", 0))
    if attempts <= max_retries:
        store.requeue_task(task_id, db_path)
        notify_event(J_load_env(), "retry", _title(task, "retrying"),
                     f"{error}\n\nattempt {attempts}/{max_retries} — requeued.", task, db_path)
        LOG.warning("task %s failed but will retry (attempt %s/%s): %s",
                    task_id, attempts, max_retries, error)
        return "retry"
    store.update_task(task_id, db_path=db_path, status="failed",
                      finished_at=_now(), result=error)
    task["status"] = "failed"
    task["result"] = error
    notify_event(J_load_env(), "error", _title(task, "failed"), error, task, db_path)
    LOG.error("task %s failed: %s", task_id, error)
    return "failed"


def _mark_step(task_id: int, key: str, idx: int, state: dict[str, Any],
               db_path: str | None, **fields: Any) -> None:
    cur = state["steps"].setdefault(key, {})
    cur.update(fields)
    store.upsert_step(task_id, key, idx, db_path=db_path, **fields)


def _mark_skipped(task_id: int, key: str, idx: int, state: dict[str, Any],
                  db_path: str | None, reason: str = "") -> None:
    state["steps"][key] = {
        "status": "skipped", "attempts": 0, "params": {}, "output": "",
        "error": reason, "started_at": _now(), "finished_at": _now(),
    }
    store.upsert_step(task_id, key, idx, db_path=db_path, status="skipped",
                      error=reason, finished_at=_now())


def _save_checkpoint(task_id: int, key: str | None, idx: int | None,
                     state: dict[str, Any], db_path: str | None, done: bool = False) -> None:
    store.update_task(task_id, db_path=db_path, checkpoint=state)
    if key is not None and idx is not None:
        row = state["steps"].get(key, {})
        store.upsert_step(task_id, key, idx, db_path=db_path,
                          status=row.get("status", "running"),
                          attempts=row.get("attempts", 0),
                          params=row.get("params"),
                          output=row.get("output"),
                          error=row.get("error"),
                          started_at=row.get("started_at"),
                          finished_at=row.get("finished_at"),
                          checkpoint=state)


def _title(task: dict[str, Any], event: str) -> str:
    return f"jorge: {task.get('name', 'task')} — {event}"


def _message(task: dict[str, Any], detail: str) -> str:
    return f"task #{task.get('id')} ({task.get('name')})\n\n{_clip(detail)}"


def _notify_on_start(env: dict[str, str], task: dict[str, Any], db_path: str | None) -> None:
    if int(task.get("attempts", 0)) <= 1:
        notify_event(env, "start", _title(task, "started"),
                     f"task #{task.get('id')} ({task.get('name')}) picked up by worker.", task, db_path)


def _worker_once(env: dict[str, str], db_path: str | None) -> bool:
    task = store.claim_next(db_path)
    if task is None:
        return False
    store.init_db(db_path)
    _notify_on_start(env, task, db_path)
    execute_task(env, task, db_path)
    return True


def worker_loop(env: dict[str, str], db_path: str | None = None,
                poll_interval: float = DEFAULT_POLL, once: bool = False) -> None:
    stop = _StopSignal()
    while not stop.set():
        try:
            ran = _worker_once(env, db_path)
        except Exception:
            LOG.exception("worker error")
            ran = False
        if once:
            break
        if not ran:
            stop.wait(poll_interval)


class _StopSignal:
    def __init__(self) -> None:
        self._flag = False
        signal.signal(signal.SIGINT, self._stop)
        signal.signal(signal.SIGTERM, self._stop)

    def _stop(self, *_: Any) -> None:
        self._flag = True

    def set(self) -> bool:
        return self._flag

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)


def J_load_env() -> dict[str, str]:
    import assistant as J
    return J.load_env()


def main(argv: list[str] | None = None) -> int:
    import argparse
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog="pursuit worker", description="Poll the jorge task queue and run workflows.")
    p.add_argument("--db", default=None, help="path to the pursuit SQLite database")
    p.add_argument("--poll", type=float, default=DEFAULT_POLL, help="seconds between polls")
    p.add_argument("--once", action="store_true", help="claim and run a single task, then exit")
    p.add_argument("--daemon", action="store_true", help="detach and run in the background (writes pursuit_worker.pid/.log)")
    p.add_argument("--reset", action="store_true", help="reset tasks left 'running' by a dead worker, then continue")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.reset:
        _reset_stale(args.db)

    if args.daemon:
        _daemonize(argv)
        return 0

    env = J_load_env()
    store.init_db(args.db)
    LOG.info("worker started (poll=%ss db=%s)", args.poll, args.db or store.DEFAULT_DB)
    try:
        worker_loop(env, args.db, args.poll, once=args.once)
    except KeyboardInterrupt:
        pass
    return 0


def _reset_stale(db_path: str | None) -> None:
    conn = store.connect(db_path)
    try:
        rows = conn.execute("SELECT id FROM tasks WHERE status = 'running'").fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            store.reset_claimed(ids, db_path)
            LOG.warning("reset %s stale running task(s): %s", len(ids), ids)
        else:
            LOG.info("no stale running tasks to reset")
    finally:
        conn.close()


def _daemonize(argv: list[str]) -> None:
    filtered = [a for a in argv if a != "--daemon"]
    log = open(LOG_FILE, "a", buffering=1)
    pid = os.fork()
    if pid > 0:
        with open(PID_FILE, "w") as f:
            f.write(str(pid))
        log.write(f"[pursuit] spawned worker pid {pid} (log: {LOG_FILE})\n")
        log.close()
        return
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
    sys.exit(main(filtered))