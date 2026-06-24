#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MediaCrawler Dashboard Server
Flask API + background snapshot collector.
Independent of crawler — survives crawler restart/exit.
"""
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

from flask import Flask, jsonify, request

from db import (
    get_crawler_db_path, get_config_values, get_crawler_stats,
    get_velocity, get_latest_note, _connect, _kw_placeholders,
)
from groups import list_groups, save_group, activate_group, delete_group
from task_manager import init_task_db
from worth_scoring import score_post

# --- config ---
PORT = 18998
HOST = "0.0.0.0"
MEDIACRAWLER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(MEDIACRAWLER_ROOT, "logs", "crawler.log")

DASHBOARD_DIR = os.path.join(MEDIACRAWLER_ROOT, "dashboard")
DASHBOARD_DB = os.path.join(DASHBOARD_DIR, "database", "dashboard.db")
STATIC_DIR = os.path.join(DASHBOARD_DIR, "static")

SNAPSHOT_INTERVAL = 30
MAX_DB_SIZE_MB = 100
MAX_HISTORY_HOURS = 72

logging.basicConfig(level=logging.INFO, format="[dashboard] %(levelname)s %(message)s")
logger = logging.getLogger("dashboard")

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
init_task_db()

# --- Auto-detect crawler DB at startup ---
_crawler_db_path = None
try:
    _crawler_db_path = get_crawler_db_path()
except FileNotFoundError as e:
    logger.warning(f"No crawler DB found: {e}")


def _with_crawler_db():
    """Context manager — yields a connection to the crawler DB, or None."""
    if not _crawler_db_path or not os.path.exists(_crawler_db_path):
        return None
    return _connect(_crawler_db_path)


# --- process info ---

def get_process_info():
    """Detect crawler process by scanning for main.py processes (any platform)."""
    try:
        # Try any main.py process first
        r = subprocess.run(
            ["pgrep", "-f", "main.py"],
            capture_output=True, text=True, timeout=5,
        )
        pids = [p for p in r.stdout.strip().split("\n") if p]
        if not pids:
            return False, "", 0.0, 0, "unknown"
        # Get platform from command line
        platform = "unknown"
        try:
            cmdline = subprocess.run(
                ["ps", "-p", pids[0], "-o", "command="],
                capture_output=True, text=True, timeout=5,
            )
            cmd = cmdline.stdout.strip()
            if "--platform" in cmd:
                for part in cmd.split():
                    if part.startswith("--platform="):
                        platform = part.split("=")[1]
                    elif part == "--platform":
                        idx = cmd.split().index(part)
                        platform = cmd.split()[idx + 1] if idx + 1 < len(cmd.split()) else "unknown"
        except Exception:
            pass
        r = subprocess.run(
            ["ps", "-p", pids[0], "-o", "etime=,cpu=,rss="],
            capture_output=True, text=True, timeout=5,
        )
        parts = r.stdout.strip().split()
        if len(parts) >= 3:
            return True, parts[0], float(parts[1]), int(parts[2]) // 1024, platform
        return True, "", 0.0, 0, platform
    except Exception:
        return False, "", 0.0, 0, "unknown"


# --- snapshot DB (dashboard's own history storage) ---

def get_db_size_mb():
    if os.path.exists(DASHBOARD_DB):
        return os.path.getsize(DASHBOARD_DB) / (1024 * 1024)
    return 0


def init_dashboard_db():
    os.makedirs(os.path.dirname(DASHBOARD_DB), exist_ok=True)
    conn = sqlite3.connect(DASHBOARD_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            total_posts INTEGER DEFAULT 0,
            total_comments INTEGER DEFAULT 0,
            process_alive INTEGER DEFAULT 0,
            process_uptime TEXT,
            process_cpu REAL,
            process_rss_mb INTEGER,
            active_keyword TEXT,
            keywords_json TEXT,
            desc_empty_count INTEGER DEFAULT 0,
            desc_empty_rate TEXT,
            rawdata_empty_count INTEGER DEFAULT 0,
            rawdata_empty_rate TEXT,
            last_crawl_ts REAL
        )
    """)
    conn.commit()
    conn.close()


def take_snapshot():
    conn = _with_crawler_db()
    if not conn:
        return
    try:
        keywords, _ = get_config_values()
        stats = get_crawler_stats(conn, keywords)
        alive, uptime, cpu, rss_mb, platform = get_process_info()
    finally:
        conn.close()

    try:
        conn2 = sqlite3.connect(DASHBOARD_DB)
        conn2.execute(
            """INSERT INTO snapshots
            (ts, total_posts, total_comments, process_alive, process_uptime,
             process_cpu, process_rss_mb, active_keyword,
             keywords_json, desc_empty_count, desc_empty_rate,
             rawdata_empty_count, rawdata_empty_rate, last_crawl_ts)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                time.time(), stats["total_posts"], stats["total_comments"],
                1 if alive else 0, uptime, cpu, rss_mb,
                stats["active_keyword"],
                json.dumps(stats["keyword_details"], ensure_ascii=False),
                stats["desc_empty_count"], stats["desc_empty_rate"],
                stats["rawdata_empty_count"], stats["rawdata_empty_rate"],
                stats["last_crawl_ts"],
            ),
        )
        conn2.commit()
        cutoff = time.time() - MAX_HISTORY_HOURS * 3600
        conn2.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,))
        conn2.commit()
        conn2.close()
    except Exception:
        try:
            conn2.close()
        except Exception:
            pass


def snapshot_loop():
    while True:
        time.sleep(SNAPSHOT_INTERVAL)
        try:
            take_snapshot()
        except Exception as e:
            logger.error(f"Snapshot failed: {e}")


# --- index (with chart.js caching) ---

_chart_js_cache = None
_chart_js_mtime = 0

def _get_chart_js():
    global _chart_js_cache, _chart_js_mtime
    chart_path = os.path.join(STATIC_DIR, "chart.min.js")
    if not os.path.exists(chart_path):
        return ""
    mtime = os.path.getmtime(chart_path)
    if _chart_js_cache is not None and mtime == _chart_js_mtime:
        return _chart_js_cache
    with open(chart_path, "r") as f:
        _chart_js_cache = f.read()
        _chart_js_mtime = mtime
        return _chart_js_cache


@app.route("/")
def index():
    html_path = os.path.join(STATIC_DIR, "dashboard.html")
    if not os.path.exists(html_path):
        return "dashboard.html not found", 404
    with open(html_path, "r") as f:
        html = f.read()
    chart_js = _get_chart_js()
    if chart_js:
        html = html.replace("<!-- CHARTJS_INLINE -->", f"<script>{chart_js}</script>")
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


# --- API ---

@app.route("/api/stats")
def api_stats():
    keywords, max_notes = get_config_values()
    conn = _with_crawler_db()
    stats = get_crawler_stats(conn, keywords) if conn else {}
    if conn:
        conn.close()

    alive, uptime, cpu, rss_mb, platform = get_process_info()
    db_size = get_db_size_mb()

    if alive:
        if stats.get("last_crawl_ts", 0) == 0:
            verdict, vcolor = "🟡 等待首次写入", "warning"
        else:
            gap = (time.time() * 1000 - stats["last_crawl_ts"]) / 1000
            if gap < 300:
                verdict, vcolor = "🟢 正常", "ok"
            elif gap < 600:
                verdict, vcolor = "🟡 缓慢", "warning"
            else:
                verdict, vcolor = f"🔴 故障（{gap/60:.0f}分钟无写入）", "error"
    else:
        verdict, vcolor = "🔴 进程已退出", "error"

    # Build keyword table rows (merged from /api/keywords)
    details = stats.get("keyword_details", {})
    keywords_table = []
    for kw, d in details.items():
        keywords_table.append({
            "keyword": kw,
            "post_count": d["post_count"],
            "comment_count": d["comment_count"],
            "status": d["status"],
            "bar_pct": min(100, d["post_count"] / max(1, max_notes) * 100),
        })

    return jsonify({
        "ts": time.time(),
        "verdict": verdict,
        "verdict_color": vcolor,
        "process": {"alive": alive, "uptime": uptime, "cpu": cpu, "rss_mb": rss_mb, "platform": platform},
        "stats": stats,
        "keywords": keywords,
        "keywords_table": keywords_table,
        "max_notes_per_keyword": max_notes,
        "db_size_mb": round(db_size, 1),
        "db_size_warning": db_size > MAX_DB_SIZE_MB,
    })


@app.route("/api/history")
def api_history():
    minutes = request.args.get("minutes", 120, type=int)
    cutoff = time.time() - minutes * 60
    try:
        conn = sqlite3.connect(DASHBOARD_DB)
        cur = conn.cursor()
        cur.execute(
            "SELECT ts, total_posts, total_comments, process_alive, active_keyword "
            "FROM snapshots WHERE ts >= ? ORDER BY ts ASC",
            (cutoff,),
        )
        rows = cur.fetchall()
        conn.close()
        return jsonify([{
            "ts": r[0], "total_posts": r[1], "total_comments": r[2],
            "process_alive": bool(r[3]), "active_keyword": r[4],
        } for r in rows])
    except Exception:
        return jsonify([])


@app.route("/api/velocity")
def api_velocity():
    keywords, _ = get_config_values()
    conn = _with_crawler_db()
    if not conn:
        return jsonify({"minute": {"data": [], "speed": {"posts": 0, "comments": 0}},
                         "hour": {"data": [], "speed": {"posts": 0, "comments": 0}}})
    try:
        minute = get_velocity(conn, keywords, "minute", 3)
        hour = get_velocity(conn, keywords, "hour", 72)
        return jsonify({"minute": minute, "hour": hour})
    finally:
        conn.close()


@app.route("/api/latest")
def api_latest():
    keywords, _ = get_config_values()
    conn = _with_crawler_db()
    if not conn:
        return jsonify({"error": "no DB connection"})
    try:
        note = get_latest_note(conn, keywords)
        return jsonify(note) if note else jsonify({"error": "no notes yet"})
    finally:
        conn.close()


@app.route("/api/health")
def api_health():
    keywords, _ = get_config_values()
    conn = _with_crawler_db()
    db_ok = conn is not None
    last_write = None
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT MAX(add_ts) FROM xhs_note")
            row = cur.fetchone()
            ts = row[0] if row and row[0] else 0
            if ts:
                last_write = round((time.time() * 1000 - ts) / 1000, 1)
        finally:
            conn.close()
    alive, _, _, _, platform = get_process_info()

    status = "ok"
    if not db_ok:
        status = "db_disconnected"
    elif not alive:
        status = "crawler_down"
    elif last_write and last_write > 600:
        status = "stalled"

    return jsonify({
        "status": status,
        "db_connected": db_ok,
        "crawler_alive": alive,
        "crawler_platform": platform,
        "last_write_seconds_ago": last_write,
    })


# --- comment quality stats ---

@app.route("/api/comment-stats")
def api_comment_stats():
    keywords, _ = get_config_values()
    conn = _with_crawler_db()
    if not conn:
        return jsonify({"posts_with_zero_comments": 0, "avg_comments_per_post": 0, "total_posts": 0, "total_comments": 0})
    try:
        cur = conn.cursor()
        ph = _kw_placeholders(keywords)

        cur.execute(
            f"SELECT COUNT(*) FROM xhs_note n WHERE n.source_keyword IN ({ph}) "
            f"AND NOT EXISTS (SELECT 1 FROM xhs_note_comment c WHERE c.note_id = n.note_id)",
            keywords,
        )
        zero_comment_posts = cur.fetchone()[0]

        cur.execute(
            f"SELECT COUNT(*) FROM xhs_note WHERE source_keyword IN ({ph})",
            keywords,
        )
        total_posts = cur.fetchone()[0] or 1

        cur.execute(
            f"SELECT COUNT(*) FROM xhs_note_comment WHERE note_id IN "
            f"(SELECT note_id FROM xhs_note WHERE source_keyword IN ({ph}))",
            keywords,
        )
        total_comments = cur.fetchone()[0] or 0

        return jsonify({
            "posts_with_zero_comments": zero_comment_posts,
            "avg_comments_per_post": round(total_comments / max(1, total_posts), 1),
            "total_posts": total_posts,
            "total_comments": total_comments,
        })
    finally:
        conn.close()


@app.route("/api/quality")
def api_quality():
    """Data quality dashboard: comment coverage, high-value gaps, etc."""
    keywords, _ = get_config_values()
    conn = _with_crawler_db()
    if not conn:
        return jsonify({"error": "no DB"})
    try:
        cur = conn.cursor()
        ph = _kw_placeholders(keywords)

        # Posts with < 5% comment capture rate
        cur.execute(f"""
            SELECT n.note_id, n.title, n.comment_count as target,
                   COUNT(c.comment_id) as actual
            FROM xhs_note n
            LEFT JOIN xhs_note_comment c ON c.note_id = n.note_id
            WHERE n.source_keyword IN ({ph})
            GROUP BY n.note_id
            HAVING n.comment_count > 50 AND (actual * 1.0 / n.comment_count) < 0.05
        """, keywords)
        low_coverage = [{"note_id": r[0], "title": r[1], "target": r[2], "actual": r[3]} for r in cur.fetchall()]

        # High-value posts (liked > 5000) missing comments
        cur.execute(f"""
            SELECT n.note_id, n.title, n.liked_count, n.comment_count
            FROM xhs_note n
            WHERE n.source_keyword IN ({ph}) AND n.liked_count > 5000
              AND NOT EXISTS (SELECT 1 FROM xhs_note_comment c WHERE c.note_id = n.note_id)
        """, keywords)
        high_value_missing = [{"note_id": r[0], "title": r[1], "liked": r[2], "target_comments": r[3]} for r in cur.fetchall()]

        return jsonify({
            "low_coverage_posts": low_coverage,
            "high_value_missing": high_value_missing,
            "low_coverage_count": len(low_coverage),
            "high_value_missing_count": len(high_value_missing),
        })
    finally:
        conn.close()


# --- task manager API ---

@app.route("/api/tasks")
def api_list_tasks():
    from task_manager import list_tasks
    return jsonify(list_tasks())


@app.route("/api/tasks/<task_id>")
def api_get_task(task_id):
    from task_manager import get_task
    task = get_task(task_id)
    if not task:
        return jsonify({"error": "not found"}), 404
    return jsonify(task)


@app.route("/api/tasks", methods=["POST"])
def api_create_task():
    from task_manager import create_task
    data = request.get_json(force=True)
    name = data.get("name", "")
    posts = data.get("posts", [])
    config = data.get("config", {})
    if not name or not isinstance(posts, list) or not posts:
        return jsonify({"error": "name and posts required"}), 400
    unique_posts = []
    seen_note_ids = set()
    for post in posts:
        note_id = post.get("note_id") if isinstance(post, dict) else None
        if not note_id or note_id in seen_note_ids:
            continue
        seen_note_ids.add(note_id)
        unique_posts.append(post)
    posts = unique_posts
    if not posts:
        return jsonify({"error": "posts must contain note_id"}), 400
    if len(posts) > 100:
        return jsonify({"error": "a task may contain at most 100 posts"}), 400
    try:
        config = {
            "batch_size": max(1, min(int(config.get("batch_size", 5)), 20)),
            "max_comments": max(1, min(int(config.get("max_comments", 200)), 500)),
            "max_sub_comments": max(0, min(int(config.get("max_sub_comments", 200)), 1000)),
            "get_sub_comments": config.get("get_sub_comments", False) is True,
            "delay": max(0, min(float(config.get("delay", 5)), 120)),
        }
    except (TypeError, ValueError):
        return jsonify({"error": "invalid task config"}), 400
    task_id = create_task(name, posts, config)
    return jsonify({"id": task_id, "ok": True})


@app.route("/api/tasks/<task_id>/start", methods=["POST"])
def api_start_task(task_id):
    from task_manager import claim_task, fail_task_start, get_task

    claimed, error = claim_task(task_id)
    if not claimed:
        status = 404 if error == "task not found" else 409
        return jsonify({"error": error}), status

    task = get_task(task_id)
    try:
        task_config = json.loads(task.get("config_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        task_config = {}

    uv = shutil.which("uv")
    command = ([uv, "run", "python"] if uv else [sys.executable]) + [
        os.path.join(DASHBOARD_DIR, "comment_fetcher.py"),
        "--task-id", task_id,
        "--batch-size", str(task_config.get("batch_size", 5)),
        "--max-comments", str(task_config.get("max_comments", 200)),
        "--max-sub-comments", str(task_config.get("max_sub_comments", 200)),
        "--delay", str(task_config.get("delay", 5)),
    ]
    if task_config.get("get_sub_comments"):
        command.append("--get-sub-comments")

    try:
        worker = subprocess.Popen(
            command,
            cwd=MEDIACRAWLER_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        fail_task_start(task_id, str(exc))
        return jsonify({"error": f"failed to start worker: {exc}"}), 500
    return jsonify({"ok": True, "pid": worker.pid, "status": "starting"}), 202


@app.route("/api/tasks/<task_id>/complete", methods=["POST"])
def api_complete_task(task_id):
    from task_manager import complete_task
    complete_task(task_id)
    return jsonify({"ok": True})


@app.route("/api/tasks/<task_id>/posts")
def api_get_task_posts(task_id):
    from task_manager import get_task_posts
    status = request.args.get("status")
    return jsonify(get_task_posts(task_id, status))


@app.route("/api/worth-digging")
def api_worth_digging():
    """Get high-value posts worth digging for comments."""
    keywords, _ = get_config_values()
    conn = _with_crawler_db()
    if not conn:
        return jsonify({"error": "no DB connection"})
    try:
        cur = conn.cursor()
        ph = _kw_placeholders(keywords)

        # Score the complete active-keyword set. Saved comment counts are
        # aggregated once to avoid one query per post.
        cur.execute(
            f"""SELECT n.note_id, n.title, n.source_keyword, n.liked_count,
                n.comment_count, LENGTH(n.desc) AS desc_length,
                COUNT(c.comment_id) AS db_comment_count
            FROM xhs_note n
            LEFT JOIN xhs_note_comment c ON c.note_id = n.note_id
            WHERE n.source_keyword IN ({ph})
            GROUP BY n.note_id""",
            keywords,
        )

        posts = []
        for r in cur.fetchall():
            post = {
                "note_id": r[0],
                "title": r[1],
                "source_keyword": r[2],
                "liked_count": r[3] or 0,
                "comment_count": r[4] or 0,
                "desc_length": r[5] or 0,
                "db_comment_count": r[6] or 0,
            }
            post.update(score_post(post))
            post.pop("desc_length")
            posts.append(post)

        posts.sort(
            key=lambda x: (x["worth_score"], x["comment_gap"], x["liked_count"]),
            reverse=True,
        )
        return jsonify({
            "total": len(posts),
            "high_priority": len([p for p in posts if p["worth_score"] >= 60]),
            "posts": posts,
        })
    finally:
        conn.close()


# --- keyword groups ---

@app.route("/api/groups")
def api_list_groups():
    return jsonify(list_groups())


@app.route("/api/groups/save", methods=["POST"])
def api_save_group():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    keywords = data.get("keywords", [])
    max_notes = data.get("max_notes", 200)
    if not name or not keywords:
        return jsonify({"error": "name and keywords required"}), 400
    save_group(name, keywords, max_notes)
    activate_group(name)
    return jsonify({"ok": True})


@app.route("/api/groups/activate", methods=["POST"])
def api_activate_group():
    data = request.get_json(force=True)
    activate_group(data.get("name", ""))
    return jsonify({"ok": True})


@app.route("/api/groups/delete", methods=["POST"])
def api_delete_group():
    data = request.get_json(force=True)
    delete_group(data.get("name", ""))
    return jsonify({"ok": True})


@app.route("/api/logs")
def api_logs():
    lines = request.args.get("lines", 100, type=int)
    lines = max(1, min(lines, 1000))
    if not os.path.exists(LOG_PATH):
        return jsonify({"error": "log file not found", "lines": []})
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        all_lines = content.split("\n")
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return jsonify({
            "lines": [l for l in tail if l.strip()],
            "total": len(all_lines),
        })
    except Exception as e:
        return jsonify({"error": str(e), "lines": []})


@app.route("/api/log-stats")
def api_log_stats():
    """Return error/warning statistics from crawler log."""
    minutes = request.args.get("minutes", 10, type=int)
    if not os.path.exists(LOG_PATH):
        return jsonify({"errors": 0, "warnings": 0, "patterns": []})
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        all_lines = content.split("\n")
        # Filter lines within time window (approximate by line count)
        window_lines = all_lines[-1000:]  # Last 1000 lines as proxy for recent
        errors = [l for l in window_lines if "ERROR" in l or "Traceback" in l]
        warnings = [l for l in window_lines if "WARNING" in l]
        # Extract error patterns
        patterns = {}
        for line in errors:
            if "LoginError" in line:
                patterns["LoginError"] = patterns.get("LoginError", 0) + 1
            elif "DataFetchError" in line:
                patterns["DataFetchError"] = patterns.get("DataFetchError", 0) + 1
            elif "IPBlockError" in line:
                patterns["IPBlockError"] = patterns.get("IPBlockError", 0) + 1
        return jsonify({
            "errors": len(errors),
            "warnings": len(warnings),
            "patterns": patterns,
            "total_lines": len(all_lines),
        })
    except Exception as e:
        return jsonify({"error": str(e), "errors": 0, "warnings": 0, "patterns": []})


# --- startup ---

if __name__ == "__main__":
    print(f"[dashboard] MediaCrawler root: {MEDIACRAWLER_ROOT}")
    print(f"[dashboard] Crawler DB: {_crawler_db_path or 'NOT FOUND'}")
    init_dashboard_db()
    take_snapshot()
    bg = threading.Thread(target=snapshot_loop, daemon=True)
    bg.start()
    print(f"[dashboard] Snapshot worker started (interval={SNAPSHOT_INTERVAL}s)")
    print(f"[dashboard] Starting on http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
