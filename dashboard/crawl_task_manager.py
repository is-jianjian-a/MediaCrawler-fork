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
    conn = sqlite3.connect(TASK_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_crawl_task_db() -> None:
    os.makedirs(os.path.dirname(TASK_DB), exist_ok=True)
    conn = _connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crawl_tasks (
            id TEXT PRIMARY KEY,
            account_id TEXT,
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
            exit_code INTEGER,
            stop_requested_at REAL,
            stop_source TEXT,
            stop_reason TEXT
        )
        """
    )
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(crawl_tasks)").fetchall()}
    for col, ddl in {
        "account_id": "ALTER TABLE crawl_tasks ADD COLUMN account_id TEXT",
        "worker_pid": "ALTER TABLE crawl_tasks ADD COLUMN worker_pid INTEGER",
        "archived_at": "ALTER TABLE crawl_tasks ADD COLUMN archived_at REAL",
        "error_message": "ALTER TABLE crawl_tasks ADD COLUMN error_message TEXT",
        "exit_code": "ALTER TABLE crawl_tasks ADD COLUMN exit_code INTEGER",
        "stop_requested_at": "ALTER TABLE crawl_tasks ADD COLUMN stop_requested_at REAL",
        "stop_source": "ALTER TABLE crawl_tasks ADD COLUMN stop_source TEXT",
        "stop_reason": "ALTER TABLE crawl_tasks ADD COLUMN stop_reason TEXT",
    }.items():
        if col not in existing_cols:
            conn.execute(ddl)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_crawl_tasks_account_status "
        "ON crawl_tasks(account_id, status)"
    )
    # Older versions stored every interrupted task as ``failed`` with exit 130.
    # Preserve the honest boundary: it was actively interrupted, but the old
    # schema did not retain who requested the stop.
    conn.execute(
        """
        UPDATE crawl_tasks
        SET status = 'cancelled',
            stop_source = COALESCE(stop_source, 'legacy_unknown'),
            stop_reason = COALESCE(stop_reason, '历史任务被主动停止，旧版本未记录停止来源')
        WHERE status = 'failed' AND exit_code = 130
        """
    )
    conn.commit()
    conn.close()


def create_crawl_task(
    name: str,
    keywords: List[str],
    config: Dict,
    account_id: str = "",
) -> str:
    task_id = f"crawl-{uuid.uuid4().hex[:8]}"
    effective_account_id = str(account_id or config.get("account_id") or "").strip()
    conn = _connect()
    conn.execute(
        """
        INSERT INTO crawl_tasks
        (id, account_id, name, status, created_at, keywords_json, config_json)
        VALUES (?, ?, ?, 'pending', ?, ?, ?)
        """,
        (
            task_id,
            effective_account_id,
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


def list_crawl_tasks(
    archived: bool = False,
    account_id: str = "",
) -> List[Dict]:
    conn = _connect()
    if archived and account_id:
        rows = conn.execute(
            "SELECT * FROM crawl_tasks WHERE archived_at IS NOT NULL AND account_id=? "
            "ORDER BY archived_at DESC",
            (account_id,),
        ).fetchall()
    elif archived:
        rows = conn.execute(
            "SELECT * FROM crawl_tasks WHERE archived_at IS NOT NULL ORDER BY archived_at DESC"
        ).fetchall()
    elif account_id:
        rows = conn.execute(
            "SELECT * FROM crawl_tasks WHERE archived_at IS NULL AND account_id=? "
            "ORDER BY created_at DESC",
            (account_id,),
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
        if row["status"] not in ("pending", "failed", "cancelled"):
            conn.rollback()
            return False, f"task is {row['status']}, expected pending/failed/cancelled"
        conn.execute(
            """
            UPDATE crawl_tasks
            SET status = 'starting', started_at = ?, completed_at = NULL,
                error_message = NULL, exit_code = NULL, worker_pid = NULL,
                stop_requested_at = NULL, stop_source = NULL, stop_reason = NULL
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
    cursor = conn.execute(
        """
        UPDATE crawl_tasks
        SET status = 'running', started_at = COALESCE(started_at, ?),
            log_path = ?, worker_pid = ?, error_message = NULL
        WHERE id = ? AND status = 'starting'
        """,
        (time.time(), log_path, worker_pid, task_id),
    )
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    if changed != 1:
        raise RuntimeError("crawl task is no longer in starting state")


def set_crawl_worker_pid(task_id: str, worker_pid: int) -> None:
    conn = _connect()
    conn.execute("UPDATE crawl_tasks SET worker_pid = ? WHERE id = ?", (worker_pid, task_id))
    conn.commit()
    conn.close()


def finish_crawl_task(task_id: str, exit_code: int, error: str = "") -> None:
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, stop_requested_at, stop_reason FROM crawl_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            return
        was_stopped = row["status"] in ("stopping", "cancelled") or row["stop_requested_at"] is not None
        if was_stopped:
            status = "cancelled"
            final_error = row["stop_reason"] or "任务被主动停止"
        else:
            status = "completed" if exit_code == 0 else "failed"
            final_error = error or None
        conn.execute(
            """
            UPDATE crawl_tasks
            SET status = ?, completed_at = ?, exit_code = ?, error_message = ?, worker_pid = NULL
            WHERE id = ?
            """,
            (status, time.time(), exit_code, final_error, task_id),
        )
        conn.commit()
    finally:
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


def request_crawl_task_stop(
    task_id: str,
    source: str = "dashboard_api",
    reason: str = "通过 Dashboard 请求停止",
) -> tuple[bool, str]:
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM crawl_tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            conn.rollback()
            return False, "task not found"
        if row["status"] not in ("starting", "running"):
            conn.rollback()
            return False, f"task is {row['status']}, cannot stop"
        conn.execute(
            """
            UPDATE crawl_tasks
            SET status = 'stopping', stop_requested_at = ?, stop_source = ?, stop_reason = ?
            WHERE id = ?
            """,
            (time.time(), source, reason, task_id),
        )
        conn.commit()
        return True, ""
    finally:
        conn.close()


def clear_crawl_task_stop_request(task_id: str, error: str) -> None:
    """Restore a task when the OS refused the stop signal."""
    conn = _connect()
    conn.execute(
        """
        UPDATE crawl_tasks
        SET status = 'running', stop_requested_at = NULL, stop_source = NULL,
            stop_reason = NULL, error_message = ?
        WHERE id = ? AND status = 'stopping'
        """,
        (error, task_id),
    )
    conn.commit()
    conn.close()


def mark_crawl_task_cancelled(task_id: str) -> None:
    """Finalize a requested stop when no worker remains to report its exit."""
    conn = _connect()
    conn.execute(
        """
        UPDATE crawl_tasks
        SET status = 'cancelled', completed_at = ?, exit_code = COALESCE(exit_code, 130),
            error_message = COALESCE(stop_reason, '任务被主动停止'), worker_pid = NULL
        WHERE id = ? AND status IN ('starting', 'running', 'stopping')
        """,
        (time.time(), task_id),
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
        if row["status"] in ("starting", "running", "stopping"):
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
