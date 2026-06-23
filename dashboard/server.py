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
import sqlite3
import subprocess
import threading
import time

from flask import Flask, jsonify, request

from db import (
    get_crawler_db_path, get_config_values, get_crawler_stats,
    get_velocity, get_latest_note, _connect, _kw_placeholders,
)
from groups import list_groups, save_group, activate_group, delete_group

# --- config ---
PORT = 18999
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
    """Detect crawler process by scanning for main.py XHS processes."""
    try:
        r = subprocess.run(
            ["pgrep", "-f", "main.py.*platform.*xhs"],
            capture_output=True, text=True, timeout=5,
        )
        pids = [p for p in r.stdout.strip().split("\n") if p]
        if not pids:
            return False, "", 0.0, 0
        r = subprocess.run(
            ["ps", "-p", pids[0], "-o", "etime=,cpu=,rss="],
            capture_output=True, text=True, timeout=5,
        )
        parts = r.stdout.strip().split()
        if len(parts) >= 3:
            return True, parts[0], float(parts[1]), int(parts[2]) // 1024
        return True, "", 0.0, 0
    except Exception:
        return False, "", 0.0, 0


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
        alive, uptime, cpu, rss_mb = get_process_info()
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

    alive, uptime, cpu, rss_mb = get_process_info()
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
        "process": {"alive": alive, "uptime": uptime, "cpu": cpu, "rss_mb": rss_mb},
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
    alive, _, _, _ = get_process_info()

    status = "ok"
    if not db_ok:
        status = "degraded"
    elif not alive:
        status = "degraded"

    return jsonify({
        "status": status,
        "db_connected": db_ok,
        "crawler_alive": alive,
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
