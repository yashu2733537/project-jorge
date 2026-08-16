"""SQLite-backed task queue with persistent state and per-step checkpoints.

Tables:
  workflows — one row per submitted workflow definition (dedup by name)
  tasks     — one row per queue entry; holds the full definition + runtime state
  steps     — one row per workflow step; holds params, output, error, checkpoint

Every step transition writes its rendered params and result back to SQLite, so a
worker can resume a partially-executed workflow from the last checkpoint after a
crash or restart.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .schema import normalize_definition

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.environ.get("PURSUIT_DB") or os.path.join(SCRIPT_DIR, "pursuit.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workflows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    definition  TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id  INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued',
    priority     INTEGER NOT NULL DEFAULT 0,
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_retries  INTEGER NOT NULL DEFAULT 0,
    payload      TEXT NOT NULL,
    vars         TEXT NOT NULL DEFAULT '{}',
    notify       TEXT NOT NULL DEFAULT '{}',
    scheduled_at TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    result       TEXT,
    checkpoint   TEXT
);

CREATE TABLE IF NOT EXISTS steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_key    TEXT NOT NULL,
    step_index  INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    params      TEXT,
    output      TEXT,
    error       TEXT,
    started_at  TEXT,
    finished_at TEXT,
    checkpoint  TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_claim
    ON tasks(status, priority, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_steps_task
    ON steps(task_id, step_index);

CREATE TABLE IF NOT EXISTS notifications (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id  INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    channel  TEXT NOT NULL,
    event    TEXT NOT NULL,
    ok       INTEGER NOT NULL DEFAULT 0,
    detail   TEXT,
    sent_at  TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | None = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _json_loads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def submit_workflow(
    definition: dict[str, Any],
    vars: dict[str, Any] | None = None,
    priority: int = 0,
    when: str | None = None,
    notify: dict[str, Any] | None = None,
    db_path: str | None = None,
) -> int:
    defn = normalize_definition(definition)
    name = defn["name"]
    now = _now()
    vars = {**defn.get("vars", {}), **(vars or {})}
    notify_cfg = {**defn.get("notify", {}), **(notify or {})}
    with connect(db_path) as conn:
        cur = conn.execute(
            "SELECT id FROM workflows WHERE name = ?", (name,)
        ).fetchone()
        if cur:
            workflow_id = cur["id"]
            conn.execute(
                "UPDATE workflows SET definition = ?, updated_at = ? WHERE id = ?",
                (json.dumps(defn), now, workflow_id),
            )
        else:
            workflow_id = conn.execute(
                "INSERT INTO workflows (name, description, definition, state, created_at, updated_at)"
                " VALUES (?, ?, ?, 'pending', ?, ?)",
                (name, defn.get("description", ""), json.dumps(defn), now, now),
            ).lastrowid
        cur = conn.execute(
            "INSERT INTO tasks (workflow_id, name, status, priority, max_retries, payload, vars, notify,"
            " scheduled_at, created_at)"
            " VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)",
            (
                workflow_id,
                name,
                priority,
                int(defn.get("retries", 0)),
                json.dumps(defn),
                json.dumps(vars),
                json.dumps(notify_cfg),
                when,
                now,
            ),
        )
        return int(cur.lastrowid)


def claim_next(db_path: str | None = None) -> dict[str, Any] | None:
    conn = connect(db_path)
    try:
        now = _now()
        with conn:
            row = conn.execute(
                "SELECT id FROM tasks"
                " WHERE status IN ('queued', 'retry')"
                "   AND (scheduled_at IS NULL OR scheduled_at <= ?)"
                " ORDER BY priority DESC, id ASC LIMIT 1",
                (now,),
            ).fetchone()
            if not row:
                return None
            task_id = row["id"]
            conn.execute(
                "UPDATE tasks SET status = 'running', attempts = attempts + 1,"
                " started_at = ?, finished_at = NULL WHERE id = ?",
                (now, task_id),
            )
        return get_task(task_id, conn=conn)
    finally:
        conn.close()


def get_task(task_id: int, conn: sqlite3.Connection | None = None,
             db_path: str | None = None) -> dict[str, Any] | None:
    own = conn is None
    c = conn or connect(db_path)
    try:
        row = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        task = _row_to_dict(row)
        if task is None:
            return None
        task["payload"] = _json_loads(task["payload"], {})
        task["vars"] = _json_loads(task["vars"], {})
        task["notify"] = _json_loads(task["notify"], {})
        task["checkpoint"] = _json_loads(task["checkpoint"], None)
        return task
    finally:
        if own:
            c.close()


def list_tasks(status: str | None = None, limit: int = 50,
               db_path: str | None = None) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT id, name, status, priority, attempts, max_retries,"
                " created_at, started_at, finished_at FROM tasks WHERE status = ?"
                " ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, name, status, priority, attempts, max_retries,"
                " created_at, started_at, finished_at FROM tasks"
                " ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]


def update_task(task_id: int, conn: sqlite3.Connection | None = None,
                db_path: str | None = None, **fields: Any) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    values = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
    own = conn is None
    c = conn or connect(db_path)
    try:
        c.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*values, task_id))
        if own:
            c.commit()
    finally:
        if own:
            c.close()


def requeue_task(task_id: int, db_path: str | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET status = 'queued', started_at = NULL WHERE id = ?",
            (task_id,),
        )


def cancel_task(task_id: int, db_path: str | None = None) -> bool:
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE tasks SET status = 'cancelled', finished_at = ? WHERE id = ?"
            " AND status IN ('queued', 'running', 'retry')",
            (_now(), task_id),
        )
        return cur.rowcount > 0


def get_steps(task_id: int, db_path: str | None = None) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM steps WHERE task_id = ? ORDER BY step_index ASC", (task_id,)
        ).fetchall()
        steps = []
        for r in rows:
            d = dict(r)
            d["params"] = _json_loads(d["params"], None)
            d["checkpoint"] = _json_loads(d["checkpoint"], None)
            steps.append(d)
        return steps


def upsert_step(task_id: int, step_key: str, step_index: int, conn: sqlite3.Connection | None = None,
                db_path: str | None = None, **fields: Any) -> None:
    row: dict[str, Any] = {"status": "pending", "attempts": 0, "params": None,
                           "output": None, "error": None, "started_at": None,
                           "finished_at": None, "checkpoint": None}
    row.update(fields)
    own = conn is None
    c = conn or connect(db_path)
    try:
        existing = c.execute(
            "SELECT id FROM steps WHERE task_id = ? AND step_key = ?", (task_id, step_key)
        ).fetchone()
        cols = ["status", "attempts", "params", "output", "error", "started_at", "finished_at", "checkpoint"]
        if existing:
            sets = ", ".join(f"{k} = ?" for k in cols)
            values = [json.dumps(row[k]) if isinstance(row[k], (dict, list)) else row[k] for k in cols]
            c.execute(f"UPDATE steps SET {sets} WHERE id = ?", (*values, existing["id"]))
        else:
            values = [task_id, step_key, step_index] + [
                json.dumps(row[k]) if isinstance(row[k], (dict, list)) else row[k] for k in cols
            ]
            c.execute(
                "INSERT INTO steps (task_id, step_key, step_index, status, attempts, params,"
                " output, error, started_at, finished_at, checkpoint)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        if own:
            c.commit()
    finally:
        if own:
            c.close()


def reset_claimed(running_tasks: list[int], db_path: str | None = None) -> None:
    if not running_tasks:
        return
    marks = ",".join("?" for _ in running_tasks)
    with connect(db_path) as conn:
        conn.execute(
            f"UPDATE tasks SET status = 'queued', started_at = NULL WHERE id IN ({marks})",
            running_tasks,
        )