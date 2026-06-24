#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dashboard data access layer.

Uses the project's existing SQLAlchemy models for schema awareness,
but keeps raw SQL for complex aggregation queries (velocity, stats).
Connection pooling via a shared sqlite3 connection per request cycle.
"""
import os
import re
import sqlite3
import sys
import time

# Ensure MediaCrawler root is on sys.path so config imports work
MEDIACRAWLER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if MEDIACRAWLER_ROOT not in sys.path:
    sys.path.insert(0, MEDIACRAWLER_ROOT)

from config.db_config import _ACCOUNT_DB_MAP


def _get_account_db_path(account: str = "02") -> str:
    """Resolve crawler DB path by account identifier."""
    if account in _ACCOUNT_DB_MAP:
        path = _ACCOUNT_DB_MAP[account]
        if os.path.exists(path):
            return path
    # Fallback: try accounts directory
    fallback = os.path.join(MEDIACRAWLER_ROOT, "database", "accounts", f"xhs_account_{account}.db")
    if os.path.exists(fallback):
        return fallback
    raise FileNotFoundError(f"No crawler DB found for account '{account}'")


def get_crawler_db_path(account: str = None) -> str:
    """Public entry point — resolve the crawler DB path.

    Priority:
    1. Explicit account parameter
    2. Default base DB (sqlite_tables.db) if it exists and has data
    3. Account 02, then 03 as fallback
    """
    if account:
        return _get_account_db_path(account)

    # Check default base DB first (contains data for default/no-account crawls)
    base_db = os.path.join(MEDIACRAWLER_ROOT, "database", "sqlite_tables.db")
    if os.path.exists(base_db) and os.path.getsize(base_db) > 0:
        return base_db

    # Fallback: try account DBs
    for acct in ["02", "03"]:
        path = _ACCOUNT_DB_MAP.get(acct, "")
        if path and os.path.exists(path):
            return path
    raise FileNotFoundError("No crawler DB found")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def fmt_count(n) -> str:
    """Format integer to human-readable string (e.g. 26000 -> '26,000')"""
    if not n:
        return "0"
    return f"{int(n):,}"


def get_config_values():
    """Extract KEYWORDS and CRAWLER_MAX_NOTES_COUNT. Active group overrides config."""
    # Check active keyword group first
    try:
        from groups import get_active_group
        keywords, max_notes = get_active_group()
        if keywords:
            return keywords, (max_notes or 200)
    except Exception:
        pass

    # Fallback: parse base_config.py
    keywords = []
    max_notes = 200
    config_path = os.path.join(MEDIACRAWLER_ROOT, "config", "base_config.py")
    try:
        with open(config_path, "r") as f:
            content = f.read()
        m = re.search(r'KEYWORDS\s*=\s*"([^"]+)"', content)
        if m:
            keywords = [kw.strip() for kw in m.group(1).split(",") if kw.strip()]
        m = re.search(r'CRAWLER_MAX_NOTES_COUNT\s*=\s*(\d+)', content)
        if m:
            max_notes = int(m.group(1))
    except Exception:
        pass
    return keywords, max_notes


def _kw_placeholders(keywords):
    return ",".join(["?"] * len(keywords))


def get_crawler_stats(db_conn: sqlite3.Connection, keywords: list) -> dict:
    stats = {
        "total_posts": 0,
        "total_comments": 0,
        "desc_empty_count": 0,
        "desc_empty_rate": "0%",
        "rawdata_empty_count": 0,
        "rawdata_empty_rate": "0%",
        "active_keyword": "",
        "last_crawl_ts": 0,
        "keyword_details": {kw: {"post_count": 0, "comment_count": 0, "status": "等待"} for kw in keywords},
    }
    if not keywords:
        return stats

    cur = db_conn.cursor()
    ph = _kw_placeholders(keywords)

    # 1. Max timestamps
    cur.execute(f"SELECT MAX(add_ts) FROM xhs_note WHERE source_keyword IN ({ph})", keywords)
    row = cur.fetchone()
    max_note_ts = row[0] if row and row[0] else 0

    cur.execute(
        f"SELECT MAX(add_ts) FROM xhs_note_comment WHERE note_id IN "
        f"(SELECT note_id FROM xhs_note WHERE source_keyword IN ({ph}))", keywords
    )
    row = cur.fetchone()
    max_comment_ts = row[0] if row and row[0] else 0
    stats["last_crawl_ts"] = max(max_note_ts, max_comment_ts)

    # 2. Per-keyword note counts + empty rates in one GROUP BY
    cur.execute(
        f"SELECT source_keyword, COUNT(*), "
        f"SUM(CASE WHEN desc IS NULL OR TRIM(desc) = '' THEN 1 ELSE 0 END), "
        f"SUM(CASE WHEN raw_data IS NULL OR raw_data = '' THEN 1 ELSE 0 END) "
        f"FROM xhs_note WHERE source_keyword IN ({ph}) GROUP BY source_keyword",
        keywords,
    )
    total_posts = 0
    total_desc_empty = 0
    total_raw_empty = 0
    for r in cur.fetchall():
        kw, cnt, desc_empty, raw_empty = r[0], r[1], r[2], r[3]
        stats["keyword_details"][kw]["post_count"] = cnt
        stats["keyword_details"][kw]["status"] = "完成"
        total_posts += cnt
        total_desc_empty += desc_empty
        total_raw_empty += raw_empty

    stats["total_posts"] = total_posts
    stats["desc_empty_count"] = total_desc_empty
    stats["desc_empty_rate"] = f"{total_desc_empty / total_posts * 100:.1f}%" if total_posts else "0%"
    stats["rawdata_empty_count"] = total_raw_empty
    stats["rawdata_empty_rate"] = f"{total_raw_empty / total_posts * 100:.1f}%" if total_posts else "0%"

    # 3. Per-keyword comment counts via JOIN
    cur.execute(
        f"SELECT n.source_keyword, COUNT(*) FROM xhs_note_comment c "
        f"JOIN xhs_note n ON c.note_id = n.note_id "
        f"WHERE n.source_keyword IN ({ph}) GROUP BY n.source_keyword",
        keywords,
    )
    total_comments = 0
    for r in cur.fetchall():
        kw, cnt = r[0], r[1]
        stats["keyword_details"][kw]["comment_count"] = cnt
        total_comments += cnt
    stats["total_comments"] = total_comments

    # 4. Active keyword + status — single GROUP BY for last 5 min
    if max_note_ts:
        cutoff = max_note_ts - 300000
        cur.execute(
            f"SELECT source_keyword, COUNT(*) FROM xhs_note "
            f"WHERE source_keyword IN ({ph}) AND add_ts >= ? "
            f"GROUP BY source_keyword ORDER BY 2 DESC",
            keywords + [cutoff],
        )
        recent_rows = cur.fetchall()
        if recent_rows:
            stats["active_keyword"] = recent_rows[0][0]
            active_set = {r[0] for r in recent_rows if r[1] > 0}
            for kw in keywords:
                if kw in active_set:
                    stats["keyword_details"][kw]["status"] = "活跃"

    return stats


def get_velocity(db_conn: sqlite3.Connection, keywords: list,
                 granularity: str = "minute", hours: int = 6) -> dict:
    if not keywords:
        return {"granularity": granularity, "data": [], "speed": {"posts": 0, "comments": 0}}

    cur = db_conn.cursor()
    ph = _kw_placeholders(keywords)
    cutoff = int(time.time() * 1000) - hours * 3600 * 1000

    if granularity == "minute":
        div = 60000
    else:
        div = 3600000

    # Posts per bucket
    cur.execute(
        f"SELECT (add_ts / {div}) * {div} as bucket, COUNT(*) "
        f"FROM xhs_note WHERE source_keyword IN ({ph}) AND add_ts >= ? "
        f"GROUP BY bucket ORDER BY bucket",
        keywords + [cutoff],
    )
    post_rows = cur.fetchall()

    # Comments per bucket
    cur.execute(
        f"SELECT (add_ts / {div}) * {div} as bucket, COUNT(*) "
        f"FROM xhs_note_comment WHERE add_ts >= ? AND note_id IN "
        f"(SELECT note_id FROM xhs_note WHERE source_keyword IN ({ph})) "
        f"GROUP BY bucket ORDER BY bucket",
        [cutoff] + keywords,
    )
    comment_rows = cur.fetchall()

    # Merge
    post_map = {row[0]: row[1] for row in post_rows}
    comment_map = {row[0]: row[1] for row in comment_rows}
    all_buckets = sorted(set(list(post_map.keys()) + list(comment_map.keys())))

    data = []
    for b in all_buckets:
        data.append({
            "ts": b / 1000,
            "posts": post_map.get(b, 0),
            "comments": comment_map.get(b, 0),
        })

    # Current speed — last 60 seconds of activity (avoid flickering to 0 on bucket boundaries)
    now_ms = int(time.time() * 1000)
    speed_cutoff = now_ms - 60000

    cur.execute(
        f"SELECT COUNT(*) FROM xhs_note WHERE source_keyword IN ({ph}) AND add_ts >= ?",
        keywords + [speed_cutoff],
    )
    speed_posts = cur.fetchone()[0]

    cur.execute(
        f"SELECT COUNT(*) FROM xhs_note_comment WHERE add_ts >= ? AND note_id IN "
        f"(SELECT note_id FROM xhs_note WHERE source_keyword IN ({ph}))",
        [speed_cutoff] + keywords,
    )
    speed_comments = cur.fetchone()[0]

    return {
        "granularity": granularity,
        "data": data,
        "speed": {"posts": speed_posts, "comments": speed_comments},
    }


def get_latest_note(db_conn: sqlite3.Connection, keywords: list) -> dict:
    if not keywords:
        return None

    cur = db_conn.cursor()
    ph = _kw_placeholders(keywords)

    cur.execute(
        f"SELECT note_id, title, desc, image_list, note_url, type, liked_count, "
        f"collected_count, comment_count, source_keyword, "
        f"datetime(add_ts/1000,'unixepoch','localtime') "
        f"FROM xhs_note WHERE source_keyword IN ({ph}) "
        f"ORDER BY add_ts DESC LIMIT 1",
        keywords,
    )
    row = cur.fetchone()
    if not row:
        return None

    note = {
        "note_id": row[0],
        "title": row[1],
        "desc": row[2] or "",
        "image_list": [u.strip().strip('"') for u in (row[3] or "").split(",") if u.strip()],
        "note_url": row[4],
        "type": row[5],
        "liked_count": fmt_count(row[6] or 0),
        "collected_count": fmt_count(row[7] or 0),
        "comment_count": fmt_count(row[8] or 0),
        "source_keyword": row[9],
        "crawl_time": row[10],
    }

    # Top-level comments
    cur.execute(
        "SELECT comment_id, content, like_count, sub_comment_count, "
        "nickname, ip_location, datetime(create_time/1000,'unixepoch','localtime') "
        "FROM xhs_note_comment WHERE note_id = ? AND parent_comment_id = '' "
        "ORDER BY create_time ASC",
        (note["note_id"],),
    )
    comments = []
    for cr in cur.fetchall():
        comments.append({
            "comment_id": cr[0],
            "content": cr[1] or "",
            "like_count": fmt_count(cr[2] or 0),
            "sub_comment_count": cr[3] or 0,
            "nickname": cr[4] or "用户",
            "ip_location": cr[5] or "",
            "time": cr[6],
            "sub_comments": [],
        })

    # Sub-comments
    if comments:
        comment_ids = [c["comment_id"] for c in comments]
        id_ph = ",".join(["?"] * len(comment_ids))
        cur.execute(
            f"SELECT comment_id, content, like_count, nickname, parent_comment_id, "
            f"datetime(create_time/1000,'unixepoch','localtime') "
            f"FROM xhs_note_comment WHERE parent_comment_id IN ({id_ph}) "
            f"ORDER BY create_time ASC",
            comment_ids,
        )
        sub_map = {}
        for sr in cur.fetchall():
            pid = sr[4]
            sub_map.setdefault(pid, []).append({
                "comment_id": sr[0],
                "content": sr[1] or "",
                "like_count": fmt_count(sr[2] or 0),
                "nickname": sr[3] or "用户",
            })
        for c in comments:
            c["sub_comments"] = sub_map.get(c["comment_id"], [])

    note["comments"] = comments
    return note
