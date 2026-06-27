#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Keyword crawl task storage for Dashboard-created search jobs."""

import json
import os
import sqlite3
import time
import uuid
from typing import Dict, List, Optional


MEDIACRAWLER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD_DIR = os.path.join(MEDIACRAWLER_ROOT, "dashboard")
TASK_DB = os.path.join(DASHBOARD_DIR, "database", "task_manager.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(TASK_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_crawl_task_db() -> None:
    os.makedirs(os.path.dirname(TASK_DB), exist_ok=True)
    conn = _connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crawl_tasks (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at REAL NOT NULL,
            started_at REAL,
            completed_at REAL,
            keywords_json TEXT NOT NULL,
            config_json TEXT NOT NULL,
            log_path TEXT,
            worker_pid INTEGER,
            archived_at REAL,
            error_message TEXT,
            exit_code INTEGER
        )
        """
    )
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(crawl_tasks)").fetchall()}
    for col, ddl in {
        "worker_pid": "ALTER TABLE crawl_tasks ADD COLUMN worker_pid INTEGER",
        "archived_at": "ALTER TABLE crawl_tasks ADD COLUMN archived_at REAL",
        "error_message": "ALTER TABLE crawl_tasks ADD COLUMN error_message TEXT",
        "exit_code": "ALTER TABLE crawl_tasks ADD COLUMN exit_code INTEGER",
    }.items():
        if col not in existing_cols:
            conn.execute(ddl)
    conn.commit()
    conn.close()


def create_crawl_task(name: str, keywords: List[str], config: Dict) -> str:
    task_id = f"crawl-{uuid.uuid4().hex[:8]}"
    conn = _connect()
    conn.execute(
        """
        INSERT INTO crawl_tasks (id, name, status, created_at, keywords_json, config_json)
        VALUES (?, ?, 'pending', ?, ?, ?)
        """,
        (
            task_id,
            name,
            time.time(),
            json.dumps(keywords, ensure_ascii=False),
            json.dumps(config, ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()
    return task_id


def _row_to_task(row: sqlite3.Row) -> Dict:
    task = dict(row)
    for key, default in (("keywords_json", []), ("config_json", {})):
        try:
            task[key[:-5]] = json.loads(task.get(key) or "null") or default
        except (TypeError, json.JSONDecodeError):
            task[key[:-5]] = default
    return task


def get_crawl_task(task_id: str) -> Optional[Dict]:
    conn = _connect()
    row = conn.execute("SELECT * FROM crawl_tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return _row_to_task(row) if row else None


def list_crawl_tasks(archived: bool = False) -> List[Dict]:
    conn = _connect()
    if archived:
        rows = conn.execute(
            "SELECT * FROM crawl_tasks WHERE archived_at IS NOT NULL ORDER BY archived_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM crawl_tasks WHERE archived_at IS NULL ORDER BY created_at DESC"
        ).fetchall()
    conn.close()
    return [_row_to_task(row) for row in rows]


def claim_crawl_task(task_id: str) -> tuple[bool, str]:
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM crawl_tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            conn.rollback()
            return False, "task not found"
        if row["status"] not in ("pending", "failed"):
            conn.rollback()
            return False, f"task is {row['status']}, expected pending/failed"
        conn.execute(
            """
            UPDATE crawl_tasks
            SET status = 'starting', started_at = ?, completed_at = NULL,
                error_message = NULL, exit_code = NULL, worker_pid = NULL
            WHERE id = ?
            """,
            (time.time(), task_id),
        )
        conn.commit()
        return True, ""
    finally:
        conn.close()


def start_crawl_task(task_id: str, log_path: str, worker_pid: int = None) -> None:
    conn = _connect()
    conn.execute(
        """
        UPDATE crawl_tasks
        SET status = 'running', started_at = COALESCE(started_at, ?),
            log_path = ?, worker_pid = ?, error_message = NULL
        WHERE id = ?
        """,
        (time.time(), log_path, worker_pid, task_id),
    )
    conn.commit()
    conn.close()


def set_crawl_worker_pid(task_id: str, worker_pid: int) -> None:
    conn = _connect()
    conn.execute("UPDATE crawl_tasks SET worker_pid = ? WHERE id = ?", (worker_pid, task_id))
    conn.commit()
    conn.close()


def finish_crawl_task(task_id: str, exit_code: int, error: str = "") -> None:
    status = "completed" if exit_code == 0 else "failed"
    conn = _connect()
    conn.execute(
        """
        UPDATE crawl_tasks
        SET status = ?, completed_at = ?, exit_code = ?, error_message = ?, worker_pid = NULL
        WHERE id = ?
        """,
        (status, time.time(), exit_code, error or None, task_id),
    )
    conn.commit()
    conn.close()


def fail_crawl_task_start(task_id: str, error: str) -> None:
    conn = _connect()
    conn.execute(
        """
        UPDATE crawl_tasks
        SET status = 'pending', error_message = ?, worker_pid = NULL
        WHERE id = ? AND status = 'starting'
        """,
        (error, task_id),
    )
    conn.commit()
    conn.close()


def mark_crawl_task_cancelled(task_id: str, error: str = "cancelled by dashboard") -> None:
    conn = _connect()
    conn.execute(
        """
        UPDATE crawl_tasks
        SET status = 'failed', completed_at = ?, error_message = ?, worker_pid = NULL
        WHERE id = ? AND status IN ('starting', 'running')
        """,
        (time.time(), error, task_id),
    )
    conn.commit()
    conn.close()


def set_crawl_task_archived(task_id: str, archived: bool) -> tuple[bool, str]:
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM crawl_tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            conn.rollback()
            return False, "task not found"
        if row["status"] in ("starting", "running"):
            conn.rollback()
            return False, f"task is {row['status']}, cannot archive"
        conn.execute(
            "UPDATE crawl_tasks SET archived_at = ? WHERE id = ?",
            (time.time() if archived else None, task_id),
        )
        conn.commit()
        return True, ""
    finally:
        conn.close()
