#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MediaCrawler Task Manager
=========================
用于管理评论补抓任务，与dashboard集成。
功能：
1. 从dashboard获取高价值帖子列表
2. 创建评论补抓任务
3. 监控任务进度
4. 与dashboard交互展示任务状态

用法：
    python task_manager.py create --name "video-comment-batch-1" --posts-file posts.json
    python task_manager.py status --task-id <id>
    python task_manager.py list
    python task_manager.py complete --task-id <id>
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional

# --- config ---
MEDIACRAWLER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD_DIR = os.path.join(MEDIACRAWLER_ROOT, "dashboard")
TASK_DB = os.path.join(DASHBOARD_DIR, "database", "task_manager.db")
CRAWLER_DB = os.path.join(MEDIACRAWLER_ROOT, "database", "sqlite_tables.db")

sys.path.insert(0, MEDIACRAWLER_ROOT)


# --- task DB ---

def init_task_db():
    """Initialize task manager SQLite DB."""
    os.makedirs(os.path.dirname(TASK_DB), exist_ok=True)
    conn = sqlite3.connect(TASK_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at REAL NOT NULL,
            started_at REAL,
            completed_at REAL,
            total_posts INTEGER DEFAULT 0,
            completed_posts INTEGER DEFAULT 0,
            failed_posts INTEGER DEFAULT 0,
            total_comments_added INTEGER DEFAULT 0,
            config_json TEXT,
            log_path TEXT,
            worker_pid INTEGER,
            archived_at REAL,
            error_message TEXT
        )
    """)
    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
    }
    if "worker_pid" not in existing_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN worker_pid INTEGER")
    if "archived_at" not in existing_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN archived_at REAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            note_id TEXT NOT NULL,
            source_keyword TEXT,
            title TEXT,
            liked_count INTEGER DEFAULT 0,
            comment_count_before INTEGER DEFAULT 0,
            comment_count_after INTEGER DEFAULT 0,
            worth_score REAL DEFAULT 0,
            status TEXT DEFAULT 'pending',
            error_message TEXT,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_task_posts_task_note "
        "ON task_posts(task_id, note_id)"
    )
    conn.commit()
    conn.close()


def create_task(name: str, posts: List[Dict], config: Dict) -> str:
    """Create a new comment supplement task."""
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    conn = sqlite3.connect(TASK_DB)

    conn.execute(
        """INSERT INTO tasks (id, name, status, created_at, total_posts, config_json)
           VALUES (?, ?, 'pending', ?, ?, ?)""",
        (task_id, name, time.time(), len(posts), json.dumps(config, ensure_ascii=False))
    )

    for post in posts:
        conn.execute(
            """INSERT INTO task_posts (task_id, note_id, source_keyword, title,
               liked_count, comment_count_before, worth_score, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (task_id, post['note_id'], post.get('source_keyword', ''),
             post.get('title', '')[:200], post.get('liked_count', 0),
             post.get('db_comment_count', post.get('comment_count_before', 0)),
             post.get('worth_score', 0))
        )

    conn.commit()
    conn.close()
    return task_id


def get_task(task_id: str) -> Optional[Dict]:
    """Get task info by ID."""
    conn = sqlite3.connect(TASK_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return None

    task = dict(row)

    # Get post stats
    cur.execute("""
        SELECT status, COUNT(*) as cnt FROM task_posts WHERE task_id = ? GROUP BY status
    """, (task_id,))
    task['post_status'] = {r['status']: r['cnt'] for r in cur.fetchall()}

    conn.close()
    return task


def list_tasks(archived: bool = False) -> List[Dict]:
    """List all tasks."""
    conn = sqlite3.connect(TASK_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if archived:
        cur.execute(
            "SELECT * FROM tasks WHERE archived_at IS NOT NULL ORDER BY archived_at DESC"
        )
    else:
        cur.execute(
            "SELECT * FROM tasks WHERE archived_at IS NULL ORDER BY created_at DESC"
        )
    tasks = [dict(r) for r in cur.fetchall()]
    for task in tasks:
        cur.execute(
            "SELECT status, COUNT(*) as cnt FROM task_posts WHERE task_id = ? GROUP BY status",
            (task["id"],),
        )
        task["post_status"] = {r["status"]: r["cnt"] for r in cur.fetchall()}
    conn.close()
    return tasks


def set_task_archived(task_id: str, archived: bool) -> tuple[bool, str]:
    """Archive or restore a task."""
    conn = sqlite3.connect(TASK_DB, timeout=10)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            conn.rollback()
            return False, "task not found"
        if row[0] in ("starting", "running"):
            conn.rollback()
            return False, f"task is {row[0]}, cannot archive"
        conn.execute(
            "UPDATE tasks SET archived_at = ? WHERE id = ?",
            (time.time() if archived else None, task_id),
        )
        conn.commit()
        return True, ""
    finally:
        conn.close()


def update_post_status(task_id: str, note_id: str, status: str,
                       comment_count_after: int = 0, error: str = None,
                       comment_count_before: int = None):
    """Update a single post's status within a task."""
    conn = sqlite3.connect(TASK_DB)
    if comment_count_before is None:
        conn.execute(
            """UPDATE task_posts SET status = ?, comment_count_after = ?,
               error_message = ? WHERE task_id = ? AND note_id = ?""",
            (status, comment_count_after, error, task_id, note_id)
        )
    else:
        conn.execute(
            """UPDATE task_posts SET status = ?,
               comment_count_before = CASE WHEN status = 'pending'
                   THEN ? ELSE comment_count_before END,
               comment_count_after = ?, error_message = ?
               WHERE task_id = ? AND note_id = ?""",
            (status, comment_count_before, comment_count_after, error, task_id, note_id)
        )

    # Update task progress
    cur = conn.cursor()
    cur.execute(
        """SELECT COUNT(*) FROM task_posts WHERE task_id = ? AND status = 'completed'""",
        (task_id,)
    )
    completed = cur.fetchone()[0]
    cur.execute(
        """SELECT COUNT(*) FROM task_posts WHERE task_id = ? AND status = 'failed'""",
        (task_id,)
    )
    failed = cur.fetchone()[0]

    cur.execute(
        """SELECT COALESCE(SUM(MAX(comment_count_after - comment_count_before, 0)), 0)
           FROM task_posts WHERE task_id = ?""", (task_id,)
    )
    comments_added = cur.fetchone()[0]
    conn.execute(
        """UPDATE tasks SET completed_posts = ?, failed_posts = ?,
           total_comments_added = ? WHERE id = ?""",
        (completed, failed, comments_added, task_id)
    )
    conn.commit()
    conn.close()


def complete_task(task_id: str):
    """Mark task as completed."""
    conn = sqlite3.connect(TASK_DB)
    conn.execute(
        """UPDATE tasks SET status = 'completed', completed_at = ? WHERE id = ?""",
        (time.time(), task_id)
    )
    conn.commit()
    conn.close()


def start_task(task_id: str, log_path: str = None):
    """Mark a task as running."""
    conn = sqlite3.connect(TASK_DB)
    conn.execute(
        """UPDATE tasks SET status = 'running', started_at = ?, log_path = ?,
           error_message = NULL WHERE id = ?""",
        (time.time(), log_path, task_id),
    )
    conn.commit()
    conn.close()


def set_task_worker_pid(task_id: str, pid: int):
    """Persist the background worker pid so the dashboard can terminate it."""
    conn = sqlite3.connect(TASK_DB, timeout=10)
    conn.execute(
        "UPDATE tasks SET worker_pid = ? WHERE id = ?",
        (pid, task_id),
    )
    conn.commit()
    conn.close()


def claim_task(task_id: str, retry_failed: bool = False) -> tuple[bool, str]:
    """Atomically reserve a pending task before launching its worker."""
    conn = sqlite3.connect(TASK_DB, timeout=10)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            conn.rollback()
            return False, "task not found"
        allowed_statuses = ("completed_with_errors", "pending") if retry_failed else ("pending",)
        if row[0] not in allowed_statuses:
            conn.rollback()
            return False, f"task is {row[0]}, expected {'/'.join(allowed_statuses)}"
        post_status = "failed" if retry_failed else "pending"
        pending = conn.execute(
            "SELECT COUNT(*) FROM task_posts WHERE task_id = ? AND status = ?",
            (task_id, post_status),
        ).fetchone()[0]
        if not pending:
            conn.rollback()
            return False, "task has no pending posts"
        conn.execute(
            """UPDATE tasks SET status = 'starting', started_at = ?,
               error_message = NULL, worker_pid = NULL WHERE id = ?""",
            (time.time(), task_id),
        )
        conn.commit()
        return True, ""
    finally:
        conn.close()


def fail_task_start(task_id: str, error: str):
    """Return a claimed task to pending when its worker cannot be spawned."""
    conn = sqlite3.connect(TASK_DB, timeout=10)
    conn.execute(
        """UPDATE tasks SET status = 'pending', error_message = ?
           WHERE id = ? AND status = 'starting'""",
        (error, task_id),
    )
    conn.commit()
    conn.close()


def reset_failed_posts(task_id: str) -> tuple[bool, str]:
    """Move failed posts back to pending for manual rerun."""
    conn = sqlite3.connect(TASK_DB, timeout=10)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            conn.rollback()
            return False, "task not found"
        if row[0] not in ("completed_with_errors", "pending"):
            conn.rollback()
            return False, f"task is {row[0]}, cannot reset failed posts"
        failed = conn.execute(
            "SELECT COUNT(*) FROM task_posts WHERE task_id = ? AND status = 'failed'",
            (task_id,),
        ).fetchone()[0]
        if not failed:
            conn.rollback()
            return False, "task has no failed posts"
        conn.execute(
            """UPDATE task_posts SET status = 'pending', error_message = NULL
               WHERE task_id = ? AND status = 'failed'""",
            (task_id,),
        )
        conn.execute(
            """UPDATE tasks SET status = 'pending', completed_at = NULL,
               failed_posts = 0, error_message = NULL, worker_pid = NULL
               WHERE id = ?""",
            (task_id,),
        )
        conn.commit()
        return True, ""
    finally:
        conn.close()


def mark_task_cancelled(task_id: str, error: str = "cancelled by dashboard"):
    """Mark a running/starting task as manually cancelled."""
    conn = sqlite3.connect(TASK_DB, timeout=10)
    conn.execute(
        """UPDATE task_posts SET status = 'failed', error_message = ?
           WHERE task_id = ? AND status = 'running'""",
        (error, task_id),
    )
    completed = conn.execute(
        "SELECT COUNT(*) FROM task_posts WHERE task_id = ? AND status = 'completed'",
        (task_id,),
    ).fetchone()[0]
    failed = conn.execute(
        "SELECT COUNT(*) FROM task_posts WHERE task_id = ? AND status = 'failed'",
        (task_id,),
    ).fetchone()[0]
    comments_added = conn.execute(
        """SELECT COALESCE(SUM(MAX(comment_count_after - comment_count_before, 0)), 0)
           FROM task_posts WHERE task_id = ?""",
        (task_id,),
    ).fetchone()[0]
    conn.execute(
        """UPDATE tasks SET status = 'completed_with_errors',
           completed_at = ?, error_message = ?, worker_pid = NULL,
           completed_posts = ?, failed_posts = ?, total_comments_added = ?
           WHERE id = ? AND status IN ('starting', 'running')""",
        (time.time(), error, completed, failed, comments_added, task_id),
    )
    conn.commit()
    conn.close()


def finish_task(task_id: str):
    """Finish a task, retaining whether any posts failed."""
    conn = sqlite3.connect(TASK_DB)
    counts = dict(conn.execute(
        "SELECT status, COUNT(*) FROM task_posts WHERE task_id = ? GROUP BY status",
        (task_id,),
    ).fetchall())
    if counts.get('pending', 0) or counts.get('running', 0):
        status = 'pending'
    elif counts.get('failed', 0):
        status = 'completed_with_errors'
    else:
        status = 'completed'
    completed_at = None if status == 'pending' else time.time()
    conn.execute(
        "UPDATE tasks SET status = ?, completed_at = ?, worker_pid = NULL WHERE id = ?",
        (status, completed_at, task_id),
    )
    conn.commit()
    conn.close()


def get_task_posts(task_id: str, status: str = None) -> List[Dict]:
    """Get posts for a task, optionally filtered by status."""
    conn = sqlite3.connect(TASK_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if status:
        cur.execute(
            """SELECT * FROM task_posts WHERE task_id = ? AND status = ? ORDER BY id ASC""",
            (task_id, status)
        )
    else:
        cur.execute(
            """SELECT * FROM task_posts WHERE task_id = ? ORDER BY id ASC""",
            (task_id,)
        )

    posts = [dict(r) for r in cur.fetchall()]
    conn.close()
    return posts


# --- CLI ---

def main():
    parser = argparse.ArgumentParser(description='MediaCrawler Task Manager')
    subparsers = parser.add_subparsers(dest='command')

    # create
    create_parser = subparsers.add_parser('create', help='Create a new task')
    create_parser.add_argument('--name', required=True, help='Task name')
    create_parser.add_argument('--posts-file', required=True, help='JSON file with posts to process')
    create_parser.add_argument('--batch-size', type=int, default=50, help='Posts per batch')
    create_parser.add_argument('--max-comments', type=int, default=200, help='Max comments per post')

    # status
    status_parser = subparsers.add_parser('status', help='Get task status')
    status_parser.add_argument('--task-id', required=True, help='Task ID')

    # list
    subparsers.add_parser('list', help='List all tasks')

    # complete
    complete_parser = subparsers.add_parser('complete', help='Mark task as completed')
    complete_parser.add_argument('--task-id', required=True, help='Task ID')

    # export
    export_parser = subparsers.add_parser('export', help='Export task posts')
    export_parser.add_argument('--task-id', required=True, help='Task ID')
    export_parser.add_argument('--output', required=True, help='Output JSON file')

    args = parser.parse_args()

    init_task_db()

    if args.command == 'create':
        with open(args.posts_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        posts = data.get('top_posts', data.get('posts', []))

        config = {
            'batch_size': args.batch_size,
            'max_comments': args.max_comments,
            'posts_file': args.posts_file
        }

        task_id = create_task(args.name, posts, config)
        print(f"Created task: {task_id}")
        print(f"Total posts: {len(posts)}")
        print(f"Batch size: {args.batch_size}")
        print(f"Max comments per post: {args.max_comments}")

    elif args.command == 'status':
        task = get_task(args.task_id)
        if not task:
            print(f"Task not found: {args.task_id}")
            return

        print(f"Task: {task['name']} ({task['id']})")
        print(f"Status: {task['status']}")
        print(f"Created: {datetime.fromtimestamp(task['created_at']).strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Total posts: {task['total_posts']}")
        print(f"Completed: {task['completed_posts']}")
        print(f"Failed: {task['failed_posts']}")
        print(f"Progress: {task['completed_posts']}/{task['total_posts']} ({task['completed_posts']/task['total_posts']*100:.1f}%)")
        print(f"Post status breakdown: {task.get('post_status', {})}")

    elif args.command == 'list':
        tasks = list_tasks()
        if not tasks:
            print("No tasks found")
            return

        print(f"{'ID':<20} {'Name':<30} {'Status':<10} {'Progress':<15} {'Created'}")
        print("-" * 100)
        for t in tasks:
            progress = f"{t['completed_posts']}/{t['total_posts']}"
            created = datetime.fromtimestamp(t['created_at']).strftime('%Y-%m-%d %H:%M')
            print(f"{t['id']:<20} {t['name'][:28]:<30} {t['status']:<10} {progress:<15} {created}")

    elif args.command == 'complete':
        complete_task(args.task_id)
        print(f"Task {args.task_id} marked as completed")

    elif args.command == 'export':
        posts = get_task_posts(args.task_id)
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(posts, f, ensure_ascii=False, indent=2)
        print(f"Exported {len(posts)} posts to {args.output}")

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
