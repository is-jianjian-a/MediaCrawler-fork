#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persistent account-level launch policy for Dashboard-managed XHS work.

The policy coordinates keyword and comment tasks through one SQLite state
machine.  It deliberately stops instead of trying to bypass a CAPTCHA.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional


MEDIACRAWLER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RISK_DB = os.path.join(MEDIACRAWLER_ROOT, "dashboard", "database", "task_manager.db")

COOLDOWN_SECONDS = int(os.getenv("MEDIACRAWLER_RISK_COOLDOWN_SECONDS", str(4 * 3600)))
TASK_GAP_SECONDS = int(os.getenv("MEDIACRAWLER_RISK_TASK_GAP_SECONDS", str(60 * 60)))
BROWSER_GAP_SECONDS = int(os.getenv("MEDIACRAWLER_RISK_BROWSER_GAP_SECONDS", str(90 * 60)))
LAUNCH_WINDOW_SECONDS = int(os.getenv("MEDIACRAWLER_RISK_LAUNCH_WINDOW_SECONDS", str(12 * 3600)))
# Historical Dashboard runs reached 20 browser-backed task sessions in a
# rolling 12-hour window without explicit platform risk.  Keep a 20% margin
# below that observed clean maximum instead of using the earlier synthetic 6.
MAX_LAUNCHES_PER_WINDOW = int(os.getenv("MEDIACRAWLER_RISK_MAX_LAUNCHES", "16"))
REQUIRED_CLEAN_CANARIES = int(os.getenv("MEDIACRAWLER_RISK_CLEAN_CANARIES", "2"))
RESERVATION_TTL_SECONDS = int(os.getenv("MEDIACRAWLER_RISK_RESERVATION_TTL_SECONDS", "300"))
RUNNING_LEASE_TTL_SECONDS = int(
    os.getenv("MEDIACRAWLER_RISK_RUNNING_LEASE_TTL_SECONDS", "180")
)
LEASE_HEARTBEAT_SECONDS = max(
    5,
    int(os.getenv("MEDIACRAWLER_RISK_LEASE_HEARTBEAT_SECONDS", "30")),
)

CANARY_MAX_POSTS = 5
CANARY_MIN_DETAIL_SLEEP = 240
CANARY_MAX_DETAIL_SLEEP = 300
COMMENT_MAX_POSTS = 2
COMMENT_MAX_COMMENTS = 5
COMMENT_MIN_INTERVAL = 90
MIN_SEARCH_SESSIONS_BETWEEN_COMMENTS = 2
_INITIALIZED_DATABASES: set[str] = set()


@dataclass(frozen=True)
class LaunchDecision:
    allowed: bool
    state: str
    reason: str = ""
    retry_at: float = 0
    canary: bool = False
    reservation_id: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(RISK_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _account_key(user_data_dir: str, platform: str = "xhs") -> str:
    profile = str(user_data_dir or "%s_user_data_dir_account02").strip()
    return f"{platform}:{profile}"


def _local_day(ts: float) -> str:
    return datetime.fromtimestamp(ts).date().isoformat()


def _next_local_midnight(ts: float) -> float:
    current = datetime.fromtimestamp(ts)
    tomorrow = datetime.combine(current.date() + timedelta(days=1), datetime.min.time())
    return tomorrow.timestamp()


def init_risk_policy_db() -> None:
    database_key = os.path.abspath(RISK_DB)
    if database_key in _INITIALIZED_DATABASES:
        return
    os.makedirs(os.path.dirname(RISK_DB), exist_ok=True)
    conn = _connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS xhs_risk_state (
            account_key TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'canary',
            clean_canaries INTEGER NOT NULL DEFAULT 0,
            cooldown_until REAL NOT NULL DEFAULT 0,
            locked_until REAL NOT NULL DEFAULT 0,
            last_task_started_at REAL NOT NULL DEFAULT 0,
            last_task_completed_at REAL NOT NULL DEFAULT 0,
            last_browser_launch_at REAL NOT NULL DEFAULT 0,
            last_risk_at REAL NOT NULL DEFAULT 0,
            risk_day TEXT NOT NULL DEFAULT '',
            risk_count_day INTEGER NOT NULL DEFAULT 0,
            active_task_id TEXT NOT NULL DEFAULT '',
            active_task_kind TEXT NOT NULL DEFAULT '',
            reservation_until REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS xhs_risk_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_key TEXT NOT NULL,
            task_id TEXT NOT NULL DEFAULT '',
            task_kind TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL,
            event_ts REAL NOT NULL,
            detail_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_xhs_risk_events_account_time
        ON xhs_risk_events(account_key, event_ts);
        CREATE INDEX IF NOT EXISTS idx_xhs_risk_events_task
        ON xhs_risk_events(task_id, event_type);
        """
    )
    # One-time/idempotent import of typed risk exits created before this
    # account-level policy existed.  This keeps a Dashboard restart from
    # forgetting an active cooldown.
    crawl_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='crawl_tasks'"
    ).fetchone()
    if crawl_table:
        historical = conn.execute(
            """SELECT id, COALESCE(completed_at, started_at, created_at), config_json
               FROM crawl_tasks WHERE exit_code=75 ORDER BY 2"""
        ).fetchall()
        for task_id, event_ts, config_json in historical:
            exists = conn.execute(
                """SELECT 1 FROM xhs_risk_events
                   WHERE task_id=? AND event_type='risk_control' LIMIT 1""",
                (task_id,),
            ).fetchone()
            if exists or not event_ts:
                continue
            try:
                config = json.loads(config_json or "{}")
            except (TypeError, json.JSONDecodeError):
                config = {}
            account_key = _account_key(config.get("user_data_dir", ""))
            conn.execute(
                """INSERT INTO xhs_risk_events
                   (account_key, task_id, task_kind, event_type, event_ts, detail_json)
                   VALUES (?, ?, 'search', 'risk_control', ?, '{"exit_code":75,"imported":true}')""",
                (account_key, task_id, float(event_ts)),
            )

    # Import one browser-launch lower bound per historical Dashboard task.  A
    # legacy task may have launched more than once, so this intentionally errs
    # on the conservative side without inventing unavailable precision.
    for table_name, task_kind in (("crawl_tasks", "search"), ("tasks", "comment")):
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
        ).fetchone()
        if not table_exists:
            continue
        for task_id, started_at, config_json in conn.execute(
            f"SELECT id, started_at, config_json FROM {table_name} WHERE started_at IS NOT NULL"
        ).fetchall():
            exists = conn.execute(
                """SELECT 1 FROM xhs_risk_events
                   WHERE task_id=? AND task_kind=? AND event_type='launch_started' LIMIT 1""",
                (task_id, task_kind),
            ).fetchone()
            if exists or not started_at:
                continue
            try:
                config = json.loads(config_json or "{}")
            except (TypeError, json.JSONDecodeError):
                config = {}
            if config.get("dry_run"):
                continue
            account_key = _account_key(config.get("user_data_dir", ""))
            conn.execute(
                """INSERT INTO xhs_risk_events
                   (account_key, task_id, task_kind, event_type, event_ts, detail_json)
                   VALUES (?, ?, ?, 'launch_started', ?, '{"imported":true,"lower_bound":true}')""",
                (account_key, task_id, task_kind, float(started_at)),
            )

    for account_row in conn.execute(
        "SELECT DISTINCT account_key FROM xhs_risk_events WHERE event_type='launch_started'"
    ).fetchall():
        account_key = account_row[0]
        latest_launch = conn.execute(
            """SELECT MAX(event_ts) FROM xhs_risk_events
               WHERE account_key=? AND event_type='launch_started'""",
            (account_key,),
        ).fetchone()[0]
        if latest_launch:
            conn.execute(
                """INSERT INTO xhs_risk_state
                   (account_key, last_task_started_at, last_browser_launch_at, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(account_key) DO UPDATE SET
                     last_task_started_at=MAX(last_task_started_at, excluded.last_task_started_at),
                     last_browser_launch_at=MAX(last_browser_launch_at, excluded.last_browser_launch_at),
                     updated_at=MAX(updated_at, excluded.updated_at)""",
                (account_key, latest_launch, latest_launch, time.time()),
            )

    for account_row in conn.execute(
        "SELECT DISTINCT account_key FROM xhs_risk_events WHERE event_type='risk_control'"
    ).fetchall():
        account_key = account_row[0]
        latest = conn.execute(
            """SELECT MAX(event_ts) FROM xhs_risk_events
               WHERE account_key=? AND event_type='risk_control'""",
            (account_key,),
        ).fetchone()[0]
        if not latest:
            continue
        latest_day = _local_day(float(latest))
        day_start = datetime.fromisoformat(latest_day).timestamp()
        day_end = day_start + 24 * 3600
        count_day = conn.execute(
            """SELECT COUNT(*) FROM xhs_risk_events
               WHERE account_key=? AND event_type='risk_control'
                 AND event_ts>=? AND event_ts<?""",
            (account_key, day_start, day_end),
        ).fetchone()[0]
        current = conn.execute(
            "SELECT last_risk_at FROM xhs_risk_state WHERE account_key=?", (account_key,)
        ).fetchone()
        if current and float(current[0] or 0) >= float(latest):
            continue
        now = time.time()
        if count_day >= 2 and day_end > now:
            state, cooldown_until, locked_until = "locked", 0, day_end
        else:
            cooldown_until = float(latest) + COOLDOWN_SECONDS
            locked_until = 0
            state = "cooldown" if cooldown_until > now else "canary"
        conn.execute(
            """INSERT INTO xhs_risk_state
               (account_key, state, clean_canaries, cooldown_until, locked_until,
                last_risk_at, risk_day, risk_count_day, updated_at)
               VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(account_key) DO UPDATE SET
                 state=excluded.state, clean_canaries=0,
                 cooldown_until=excluded.cooldown_until, locked_until=excluded.locked_until,
                 last_risk_at=excluded.last_risk_at, risk_day=excluded.risk_day,
                 risk_count_day=excluded.risk_count_day, updated_at=excluded.updated_at""",
            (account_key, state, cooldown_until, locked_until, latest, latest_day, count_day, now),
        )
    conn.commit()
    conn.close()
    _INITIALIZED_DATABASES.add(database_key)


def _ensure_state(conn: sqlite3.Connection, account_key: str, now: float) -> sqlite3.Row:
    conn.execute(
        """
        INSERT OR IGNORE INTO xhs_risk_state
        (account_key, state, clean_canaries, updated_at)
        VALUES (?, 'canary', 0, ?)
        """,
        (account_key, now),
    )
    return conn.execute(
        "SELECT * FROM xhs_risk_state WHERE account_key = ?", (account_key,)
    ).fetchone()


def _effective_state(row: sqlite3.Row, now: float) -> str:
    if row["locked_until"] > now:
        return "locked"
    if row["cooldown_until"] > now:
        return "cooldown"
    if row["clean_canaries"] < REQUIRED_CLEAN_CANARIES:
        return "canary"
    return "normal"


def _validate_scope(task_kind: str, config: Dict[str, Any], total_posts: int, canary: bool) -> str:
    if int(config.get("max_concurrency", 1) or 1) != 1:
        return "风控策略要求并发数为 1"
    if bool(config.get("enable_cdp")) or bool(config.get("require_cdp")):
        return "风控策略禁止 Dashboard 任务使用 CDP"
    browser_path = str(config.get("browser_path", "") or "")
    if "/Applications/Google Chrome.app/" in browser_path:
        return "风控策略禁止启动系统 Chrome"

    if task_kind == "comment":
        if total_posts > COMMENT_MAX_POSTS:
            return f"评论任务每次最多 {COMMENT_MAX_POSTS} 篇"
        if int(config.get("max_comments", 0) or 0) > COMMENT_MAX_COMMENTS:
            return f"评论任务每篇最多 {COMMENT_MAX_COMMENTS} 条"
        if int(config.get("comment_sleep", 0) or 0) < COMMENT_MIN_INTERVAL:
            return f"评论请求间隔不得低于 {COMMENT_MIN_INTERVAL} 秒"
        return ""

    get_comments = bool(config.get("get_comments"))
    if canary:
        # Search runners automatically clamp a queued task to the canary
        # envelope.  This is what makes recovery possible without an operator
        # editing an old task after the cooldown expires.
        return ""
    elif get_comments:
        if int(config.get("max_count", 0) or 0) > COMMENT_MAX_POSTS:
            return f"带评论的搜索任务最多抓取 {COMMENT_MAX_POSTS} 篇"
        if int(config.get("max_comments", 0) or 0) > COMMENT_MAX_COMMENTS:
            return f"带评论的搜索任务每篇最多 {COMMENT_MAX_COMMENTS} 条"
        if int(config.get("comment_sleep", 0) or 0) < COMMENT_MIN_INTERVAL:
            return f"评论请求间隔不得低于 {COMMENT_MIN_INTERVAL} 秒"
    return ""


def reserve_launch(
    *,
    task_id: str,
    task_kind: str,
    config: Optional[Dict[str, Any]] = None,
    total_posts: int = 0,
    now: Optional[float] = None,
) -> LaunchDecision:
    """Atomically gate and reserve one browser-backed task launch."""
    init_risk_policy_db()
    now = time.time() if now is None else float(now)
    config = config or {}
    account_key = _account_key(config.get("user_data_dir", ""))
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _ensure_state(conn, account_key, now)
        if row["active_task_id"] and row["reservation_until"] > now:
            conn.rollback()
            return LaunchDecision(False, _effective_state(row, now), "账号已有任务启动或运行中", row["reservation_until"])
        if row["active_task_id"]:
            conn.execute(
                """UPDATE xhs_risk_state SET active_task_id='', active_task_kind='',
                   reservation_until=0 WHERE account_key=?""",
                (account_key,),
            )
            row = _ensure_state(conn, account_key, now)

        state = _effective_state(row, now)
        if state == "locked":
            conn.rollback()
            return LaunchDecision(False, state, "当天第二次触发风控，已熔断至次日", row["locked_until"])
        if state == "cooldown":
            conn.rollback()
            return LaunchDecision(False, state, "触发 461/471 后处于 4 小时冷却期", row["cooldown_until"])

        if row["last_task_completed_at"] and now - row["last_task_completed_at"] < TASK_GAP_SECONDS:
            retry_at = row["last_task_completed_at"] + TASK_GAP_SECONDS
            conn.rollback()
            return LaunchDecision(False, state, "距上一任务完成不足 60 分钟", retry_at)
        if row["last_browser_launch_at"] and now - row["last_browser_launch_at"] < BROWSER_GAP_SECONDS:
            retry_at = row["last_browser_launch_at"] + BROWSER_GAP_SECONDS
            conn.rollback()
            return LaunchDecision(False, state, "距上次浏览器启动不足 90 分钟", retry_at)

        launch_count = conn.execute(
            """SELECT COUNT(*) FROM xhs_risk_events
               WHERE account_key=? AND event_type='launch_started' AND event_ts>=?""",
            (account_key, now - LAUNCH_WINDOW_SECONDS),
        ).fetchone()[0]
        if launch_count >= MAX_LAUNCHES_PER_WINDOW:
            threshold_event = conn.execute(
                """SELECT event_ts FROM xhs_risk_events
                   WHERE account_key=? AND event_type='launch_started' AND event_ts>=?
                   ORDER BY event_ts ASC LIMIT 1 OFFSET ?""",
                (account_key, now - LAUNCH_WINDOW_SECONDS, launch_count - MAX_LAUNCHES_PER_WINDOW),
            ).fetchone()[0]
            retry_at = float(threshold_event or now) + LAUNCH_WINDOW_SECONDS
            conn.rollback()
            return LaunchDecision(
                False,
                state,
                f"12 小时内浏览器任务会话已达到 {MAX_LAUNCHES_PER_WINDOW} 次",
                retry_at,
            )

        canary = state == "canary"
        if task_kind == "comment" and row["clean_canaries"] < REQUIRED_CLEAN_CANARIES:
            conn.rollback()
            return LaunchDecision(False, state, "评论任务需先完成两次干净搜索探针")
        scope_error = _validate_scope(task_kind, config, total_posts, canary)
        if scope_error:
            conn.rollback()
            return LaunchDecision(False, state, scope_error)
        if task_kind == "comment":
            last_comment = conn.execute(
                """SELECT MAX(event_ts) FROM xhs_risk_events
                   WHERE account_key=? AND event_type='launch_started'
                     AND task_kind='comment'""",
                (account_key,),
            ).fetchone()[0]
            batch_floor = max(float(last_comment or 0), float(row["last_risk_at"] or 0))
            search_sessions = conn.execute(
                """SELECT COUNT(*) FROM xhs_risk_events
                   WHERE account_key=? AND event_type='launch_started'
                     AND task_kind='search' AND event_ts>?""",
                (account_key, batch_floor),
            ).fetchone()[0]
            if search_sessions < MIN_SEARCH_SESSIONS_BETWEEN_COMMENTS:
                conn.rollback()
                return LaunchDecision(
                    False,
                    state,
                    f"评论任务需先累计 {MIN_SEARCH_SESSIONS_BETWEEN_COMMENTS} 个新搜索会话后再合并启动",
                )

        reservation_id = f"{task_id}:{int(now * 1000)}"
        conn.execute(
            """UPDATE xhs_risk_state
               SET state=?, active_task_id=?, active_task_kind=?, reservation_until=?, updated_at=?
               WHERE account_key=?""",
            (state, task_id, task_kind, now + RESERVATION_TTL_SECONDS, now, account_key),
        )
        conn.execute(
            """INSERT INTO xhs_risk_events
               (account_key, task_id, task_kind, event_type, event_ts, detail_json)
               VALUES (?, ?, ?, 'launch_reserved', ?, ?)""",
            (account_key, task_id, task_kind, now, json.dumps({"reservation_id": reservation_id, "canary": canary})),
        )
        conn.commit()
        return LaunchDecision(True, state, canary=canary, reservation_id=reservation_id)
    finally:
        conn.close()


def confirm_launch(task_id: str, user_data_dir: str, now: Optional[float] = None) -> None:
    init_risk_policy_db()
    now = time.time() if now is None else float(now)
    account_key = _account_key(user_data_dir)
    conn = _connect()
    conn.execute("BEGIN IMMEDIATE")
    row = _ensure_state(conn, account_key, now)
    if row["active_task_id"] == task_id:
        conn.execute(
            """UPDATE xhs_risk_state SET last_task_started_at=?, last_browser_launch_at=?,
               reservation_until=?, updated_at=? WHERE account_key=?""",
            (now, now, now + RUNNING_LEASE_TTL_SECONDS, now, account_key),
        )
        conn.execute(
            """INSERT INTO xhs_risk_events
               (account_key, task_id, task_kind, event_type, event_ts)
               VALUES (?, ?, ?, 'launch_started', ?)""",
            (account_key, task_id, row["active_task_kind"], now),
        )
    conn.commit()
    conn.close()


def heartbeat_launch(
    task_id: str,
    user_data_dir: str,
    now: Optional[float] = None,
) -> bool:
    """Extend a running lease only when ``task_id`` still owns the account."""
    init_risk_policy_db()
    now = time.time() if now is None else float(now)
    account_key = _account_key(user_data_dir)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _ensure_state(conn, account_key, now)
        if row["active_task_id"] != task_id:
            conn.rollback()
            return False
        conn.execute(
            """UPDATE xhs_risk_state
               SET reservation_until=?, updated_at=?
               WHERE account_key=? AND active_task_id=?""",
            (now + RUNNING_LEASE_TTL_SECONDS, now, account_key, task_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


@contextmanager
def launch_lease_heartbeat(
    task_id: str,
    user_data_dir: str,
    *,
    interval_seconds: Optional[int] = None,
):
    """Keep a confirmed launch lease alive for one worker lifecycle."""
    interval = max(5, int(interval_seconds or LEASE_HEARTBEAT_SECONDS))
    stopped = threading.Event()

    def _run() -> None:
        while not stopped.wait(interval):
            try:
                if not heartbeat_launch(task_id, user_data_dir):
                    return
            except Exception:
                # The OS profile lock remains the last line of defence.  A
                # transient task-database lock must not kill a healthy crawl.
                continue

    thread = threading.Thread(
        target=_run,
        name=f"xhs-risk-heartbeat-{task_id}",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=min(5, interval))


def abort_launch(task_id: str, user_data_dir: str, reason: str = "") -> None:
    init_risk_policy_db()
    now = time.time()
    account_key = _account_key(user_data_dir)
    conn = _connect()
    conn.execute("BEGIN IMMEDIATE")
    row = _ensure_state(conn, account_key, now)
    if row["active_task_id"] == task_id:
        conn.execute(
            """UPDATE xhs_risk_state SET active_task_id='', active_task_kind='',
               reservation_until=0, updated_at=? WHERE account_key=?""",
            (now, account_key),
        )
        conn.execute(
            """INSERT INTO xhs_risk_events
               (account_key, task_id, task_kind, event_type, event_ts, detail_json)
               VALUES (?, ?, ?, 'launch_aborted', ?, ?)""",
            (account_key, task_id, row["active_task_kind"], now, json.dumps({"reason": reason}, ensure_ascii=False)),
        )
    conn.commit()
    conn.close()


def record_completion(
    *,
    task_id: str,
    task_kind: str,
    user_data_dir: str,
    exit_code: int,
    now: Optional[float] = None,
) -> dict:
    """Persist completion and transition cooldown/canary/normal state."""
    init_risk_policy_db()
    now = time.time() if now is None else float(now)
    account_key = _account_key(user_data_dir)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _ensure_state(conn, account_key, now)
        already_recorded = conn.execute(
            """SELECT 1 FROM xhs_risk_events
               WHERE account_key=? AND task_id=?
                 AND event_type IN ('task_succeeded', 'task_failed', 'task_cancelled', 'risk_control')
               LIMIT 1""",
            (account_key, task_id),
        ).fetchone()
        if already_recorded:
            conn.rollback()
            return get_status(user_data_dir, now=now)

        current_owner = str(row["active_task_id"] or "")
        owner_mismatch = bool(current_owner and current_owner != task_id)
        if owner_mismatch and exit_code != 75:
            conn.execute(
                """INSERT INTO xhs_risk_events
                   (account_key, task_id, task_kind, event_type, event_ts, detail_json)
                   VALUES (?, ?, ?, 'completion_ignored_owner_mismatch', ?, ?)""",
                (
                    account_key,
                    task_id,
                    task_kind,
                    now,
                    json.dumps(
                        {"exit_code": exit_code, "current_owner": current_owner},
                        ensure_ascii=False,
                    ),
                ),
            )
            conn.commit()
            return get_status(user_data_dir, now=now)
        state = _effective_state(row, now)
        clean = int(row["clean_canaries"])
        cooldown_until = float(row["cooldown_until"])
        locked_until = float(row["locked_until"])
        risk_day = row["risk_day"]
        risk_count = int(row["risk_count_day"])
        event_type = (
            "task_succeeded"
            if exit_code == 0
            else ("task_cancelled" if exit_code == 130 else "task_failed")
        )

        if exit_code == 75:
            today = _local_day(now)
            risk_count = risk_count + 1 if risk_day == today else 1
            risk_day = today
            clean = 0
            event_type = "risk_control"
            if risk_count >= 2:
                locked_until = _next_local_midnight(now)
                cooldown_until = 0
                state = "locked"
            else:
                cooldown_until = now + COOLDOWN_SECONDS
                locked_until = 0
                state = "cooldown"
        elif exit_code == 0 and task_kind == "search" and clean < REQUIRED_CLEAN_CANARIES:
            clean = min(REQUIRED_CLEAN_CANARIES, clean + 1)
            state = "normal" if clean >= REQUIRED_CLEAN_CANARIES else "canary"
            cooldown_until = 0
        else:
            state = _effective_state(row, now)

        release_owner = current_owner in ("", task_id)
        next_active_task_id = "" if release_owner else current_owner
        next_active_task_kind = "" if release_owner else str(row["active_task_kind"] or "")
        next_reservation_until = 0 if release_owner else float(row["reservation_until"])
        conn.execute(
            """UPDATE xhs_risk_state
               SET state=?, clean_canaries=?, cooldown_until=?, locked_until=?,
                   last_task_completed_at=?, last_risk_at=?, risk_day=?, risk_count_day=?,
                   active_task_id=?, active_task_kind=?, reservation_until=?, updated_at=?
               WHERE account_key=?""",
            (
                state, clean, cooldown_until, locked_until, now,
                now if exit_code == 75 else row["last_risk_at"], risk_day, risk_count,
                next_active_task_id, next_active_task_kind, next_reservation_until,
                now, account_key,
            ),
        )
        conn.execute(
            """INSERT INTO xhs_risk_events
               (account_key, task_id, task_kind, event_type, event_ts, detail_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (account_key, task_id, task_kind, event_type, now, json.dumps({"exit_code": exit_code})),
        )
        conn.commit()
        return get_status(user_data_dir, now=now)
    finally:
        conn.close()


def get_status(user_data_dir: str = "%s_user_data_dir_account02", now: Optional[float] = None) -> dict:
    init_risk_policy_db()
    now = time.time() if now is None else float(now)
    account_key = _account_key(user_data_dir)
    conn = _connect()
    row = _ensure_state(conn, account_key, now)
    state = _effective_state(row, now)
    launches = conn.execute(
        """SELECT COUNT(*) FROM xhs_risk_events
           WHERE account_key=? AND event_type='launch_started' AND event_ts>=?""",
        (account_key, now - LAUNCH_WINDOW_SECONDS),
    ).fetchone()[0]
    launch_budget_retry_at = 0.0
    if launches >= MAX_LAUNCHES_PER_WINDOW:
        threshold_event = conn.execute(
            """SELECT event_ts FROM xhs_risk_events
               WHERE account_key=? AND event_type='launch_started' AND event_ts>=?
               ORDER BY event_ts ASC LIMIT 1 OFFSET ?""",
            (account_key, now - LAUNCH_WINDOW_SECONDS, launches - MAX_LAUNCHES_PER_WINDOW),
        ).fetchone()[0]
        launch_budget_retry_at = float(threshold_event or 0) + LAUNCH_WINDOW_SECONDS
    conn.commit()
    conn.close()
    return {
        "account_key": account_key,
        "state": state,
        "clean_canaries": int(row["clean_canaries"]),
        "required_clean_canaries": REQUIRED_CLEAN_CANARIES,
        "cooldown_until": float(row["cooldown_until"]),
        "locked_until": float(row["locked_until"]),
        "last_task_started_at": float(row["last_task_started_at"]),
        "last_task_completed_at": float(row["last_task_completed_at"]),
        "last_browser_launch_at": float(row["last_browser_launch_at"]),
        "last_risk_at": float(row["last_risk_at"]),
        "risk_count_day": int(row["risk_count_day"]),
        "active_task_id": row["active_task_id"],
        "active_task_kind": row["active_task_kind"],
        "lease_expires_at": float(row["reservation_until"]),
        "launches_12h": int(launches),
        "max_launches_12h": MAX_LAUNCHES_PER_WINDOW,
        "launch_budget_retry_at": launch_budget_retry_at,
        "can_run_at": max(
            now,
            float(row["cooldown_until"]),
            float(row["locked_until"]),
            float(row["last_task_completed_at"]) + TASK_GAP_SECONDS if row["last_task_completed_at"] else 0,
            float(row["last_browser_launch_at"]) + BROWSER_GAP_SECONDS if row["last_browser_launch_at"] else 0,
            launch_budget_retry_at,
        ),
        "now": now,
    }
