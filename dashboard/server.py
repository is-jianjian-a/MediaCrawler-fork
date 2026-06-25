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
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import asyncio

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
MIN_COMMENT_TASK_BATCH_SIZE = int(os.getenv("MEDIACRAWLER_MIN_COMMENT_TASK_BATCH_SIZE", "5"))

logging.basicConfig(level=logging.INFO, format="[dashboard] %(levelname)s %(message)s")
logger = logging.getLogger("dashboard")

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
init_task_db()


def _cdp_debug_port() -> int:
    try:
        return int(os.getenv("MEDIACRAWLER_CDP_DEBUG_PORT", "9222"))
    except ValueError:
        return 9222


def _is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


async def _verify_playwright_cdp(port: int, timeout: float = 3.0):
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            await playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}",
                timeout=timeout * 1000,
            )
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _check_playwright_cdp(port: int, timeout: float = 3.0):
    try:
        return asyncio.run(_verify_playwright_cdp(port, timeout))
    except Exception as exc:
        return False, str(exc)


def _find_available_cdp_port(start_port: int, max_attempts: int = 20) -> int:
    for port in range(start_port, start_port + max_attempts):
        status = check_cdp_remote_debugging(timeout=0.5, port=port, verify_playwright=True)
        if status.get("ok"):
            return port
        if not _is_port_open(port):
            return port
    raise RuntimeError(f"no available CDP port found from {start_port}")


def _chrome_binary_path() -> str:
    configured = os.getenv("MEDIACRAWLER_BROWSER_PATH", "")
    candidates = [
        configured,
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        shutil.which("google-chrome") or "",
        shutil.which("chromium") or "",
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    raise RuntimeError("Chrome binary not found. Set MEDIACRAWLER_BROWSER_PATH.")


def check_cdp_remote_debugging(timeout: float = 2.0, port: int = None, verify_playwright: bool = False):
    port = port or _cdp_debug_port()
    url = f"http://127.0.0.1:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return {
                    "ok": False,
                    "port": port,
                    "url": url,
                    "error": f"HTTP {response.status}",
                }
            data = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "port": port,
            "url": url,
            "error": str(exc),
            "hint": (
                "Chrome remote debugging is not reachable. Open Chrome and enable "
                "chrome://inspect/#remote-debugging, or start Chrome with "
                f"--remote-debugging-port={port}."
            ),
        }
    except Exception as exc:
        return {"ok": False, "port": port, "url": url, "error": str(exc)}
    browser = data.get("Browser", "")
    web_socket = data.get("webSocketDebuggerUrl", "")
    ok = bool(browser or web_socket)
    result = {
        "ok": ok,
        "port": port,
        "url": url,
        "browser": browser,
        "webSocketDebuggerUrl": web_socket,
        "raw": data,
    }
    if verify_playwright and ok:
        playwright_ok, playwright_error = _check_playwright_cdp(port, timeout=min(timeout, 3.0))
        result["playwright_ok"] = playwright_ok
        if not playwright_ok:
            result["ok"] = False
            result["error"] = playwright_error
            result["hint"] = (
                "CDP endpoint is reachable, but Playwright cannot connect to it. "
                "Use the Dashboard CDP Chrome launcher to start a compatible remote-debugging browser."
            )
    return result


def start_cdp_chrome():
    start_port = _cdp_debug_port()
    existing = check_cdp_remote_debugging(port=start_port, verify_playwright=True)
    if existing.get("ok"):
        return {**existing, "started": False, "message": "CDP already available"}

    port = _find_available_cdp_port(start_port)
    reusable = check_cdp_remote_debugging(port=port, verify_playwright=True)
    if reusable.get("ok"):
        os.environ["MEDIACRAWLER_CDP_DEBUG_PORT"] = str(port)
        return {**reusable, "started": False, "message": "CDP already available"}

    user_data_dir = os.path.join(MEDIACRAWLER_ROOT, "browser_data", f"chrome-cdp-debug-{port}")
    os.makedirs(user_data_dir, exist_ok=True)
    chrome = _chrome_binary_path()
    command = [
        chrome,
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=0.0.0.0",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=TranslateUI",
        "--disable-ipc-flooding-protection",
        "--disable-hang-monitor",
        "--disable-prompt-on-repost",
        "--disable-sync",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--exclude-switches=enable-automation",
        "--disable-infobars",
        "--start-maximized",
        f"--user-data-dir={user_data_dir}",
        "https://www.xiaohongshu.com",
    ]
    subprocess.Popen(
        command,
        cwd=MEDIACRAWLER_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    os.environ["MEDIACRAWLER_CDP_DEBUG_PORT"] = str(port)

    deadline = time.time() + 10
    last_status = None
    while time.time() < deadline:
        last_status = check_cdp_remote_debugging(timeout=1, port=port, verify_playwright=True)
        if last_status.get("ok"):
            return {
                **last_status,
                "started": True,
                "message": "CDP Chrome started",
                "user_data_dir": user_data_dir,
            }
        time.sleep(0.5)

    raise RuntimeError(
        f"Chrome was launched but CDP did not become ready on port {port}: "
        f"{(last_status or {}).get('error') or (last_status or {}).get('hint') or 'unknown error'}"
    )

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


@app.route("/api/activity")
def api_activity():
    """Latest activity feed — posts and comments interleaved by add_ts.

    Mode-aware: whatever the crawler is producing right now (posts or
    comments) bubbles to the top, so the panel always reflects real
    crawler output instead of freezing on a stale note during comment
    supplementation.
    """
    keywords, _ = get_config_values()
    conn = _with_crawler_db()
    if not conn:
        return jsonify({"items": [], "now": time.time(), "error": "no DB"})
    if not keywords:
        return jsonify({"items": [], "now": time.time()})
    try:
        cur = conn.cursor()
        ph = _kw_placeholders(keywords)
        limit = request.args.get("limit", 12, type=int)
        limit = max(1, min(limit, 50))

        # Latest posts
        cur.execute(
            f"SELECT note_id, title, source_keyword, liked_count, comment_count, "
            f"image_list, note_url, add_ts "
            f"FROM xhs_note WHERE source_keyword IN ({ph}) "
            f"ORDER BY add_ts DESC LIMIT ?",
            keywords + [limit],
        )
        items = []
        for r in cur.fetchall():
            items.append({
                "type": "post",
                "ts": (r[7] or 0) / 1000,
                "note_id": r[0],
                "title": r[1] or "(无标题)",
                "source_keyword": r[2] or "",
                "liked_count": r[3] or 0,
                "comment_count": r[4] or 0,
                "image": ([u.strip().strip('"') for u in (r[5] or "").split(",") if u.strip()] or [None])[0],
                "note_url": r[6],
            })

        # Latest comments (joined to parent note for context)
        cur.execute(
            f"SELECT c.comment_id, c.content, c.nickname, c.like_count, "
            f"c.ip_location, c.add_ts, n.title, n.source_keyword, c.note_id "
            f"FROM xhs_note_comment c JOIN xhs_note n ON c.note_id = n.note_id "
            f"WHERE n.source_keyword IN ({ph}) "
            f"ORDER BY c.add_ts DESC LIMIT ?",
            keywords + [limit],
        )
        for r in cur.fetchall():
            items.append({
                "type": "comment",
                "ts": (r[5] or 0) / 1000,
                "comment_id": r[0],
                "content": r[1] or "",
                "nickname": r[2] or "用户",
                "like_count": r[3] or 0,
                "ip_location": r[4] or "",
                "note_title": r[6] or "(无标题)",
                "source_keyword": r[7] or "",
                "note_id": r[8],
            })

        items.sort(key=lambda x: x["ts"], reverse=True)
        return jsonify({"items": items[:limit], "now": time.time()})
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


@app.route("/api/cdp/status")
def api_cdp_status():
    return jsonify(check_cdp_remote_debugging(verify_playwright=True))


@app.route("/api/cdp/start", methods=["POST"])
def api_cdp_start():
    try:
        return jsonify(start_cdp_chrome())
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


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
    archived = request.args.get("archived") in ("1", "true", "yes")
    return jsonify(list_tasks(archived=archived))


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


def _launch_comment_task(task_id, retry_failed=False):
    from task_manager import claim_task, fail_task_start, get_task, set_task_worker_pid

    claimed, error = claim_task(task_id, retry_failed=retry_failed)
    if not claimed:
        status = 404 if error == "task not found" else 409
        return jsonify({"error": error}), status

    cdp_status = check_cdp_remote_debugging(verify_playwright=True)
    if not cdp_status.get("ok"):
        message = cdp_status.get("hint") or cdp_status.get("error") or "CDP remote debugging is not ready"
        fail_task_start(task_id, message)
        return jsonify({
            "error": "CDP remote debugging is not ready",
            "detail": message,
            "cdp": cdp_status,
        }), 409

    task = get_task(task_id)
    try:
        task_config = json.loads(task.get("config_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        task_config = {}
    effective_batch_size = max(
        MIN_COMMENT_TASK_BATCH_SIZE,
        min(int(task_config.get("batch_size", 5) or 5), 20),
    )

    uv = shutil.which("uv")
    command = ([uv, "run", "python"] if uv else [sys.executable]) + [
        os.path.join(DASHBOARD_DIR, "comment_fetcher.py"),
        "--task-id", task_id,
        "--batch-size", str(effective_batch_size),
        "--max-comments", str(task_config.get("max_comments", 200)),
        "--max-sub-comments", str(task_config.get("max_sub_comments", 200)),
        "--delay", str(task_config.get("delay", 5)),
    ]
    if retry_failed:
        command.append("--retry-failed")
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
        set_task_worker_pid(task_id, worker.pid)
    except Exception as exc:
        fail_task_start(task_id, str(exc))
        return jsonify({"error": f"failed to start worker: {exc}"}), 500
    return jsonify({"ok": True, "pid": worker.pid, "status": "starting"}), 202


@app.route("/api/tasks/<task_id>/start", methods=["POST"])
def api_start_task(task_id):
    retry_failed = request.args.get("retry_failed") in ("1", "true", "yes")
    return _launch_comment_task(task_id, retry_failed=retry_failed)


@app.route("/api/tasks/<task_id>/retry-failed", methods=["POST"])
def api_retry_failed_task(task_id):
    return _launch_comment_task(task_id, retry_failed=True)


@app.route("/api/tasks/<task_id>/reset-failed", methods=["POST"])
def api_reset_failed_task(task_id):
    from task_manager import reset_failed_posts
    ok, error = reset_failed_posts(task_id)
    if not ok:
        status = 404 if error == "task not found" else 409
        return jsonify({"error": error}), status
    return jsonify({"ok": True, "status": "pending"})


@app.route("/api/tasks/<task_id>/archive", methods=["POST"])
def api_archive_task(task_id):
    from task_manager import set_task_archived
    ok, error = set_task_archived(task_id, archived=True)
    if not ok:
        status = 404 if error == "task not found" else 409
        return jsonify({"error": error}), status
    return jsonify({"ok": True, "archived": True})


@app.route("/api/tasks/<task_id>/unarchive", methods=["POST"])
def api_unarchive_task(task_id):
    from task_manager import set_task_archived
    ok, error = set_task_archived(task_id, archived=False)
    if not ok:
        status = 404 if error == "task not found" else 409
        return jsonify({"error": error}), status
    return jsonify({"ok": True, "archived": False})


@app.route("/api/tasks/<task_id>/cancel", methods=["POST"])
def api_cancel_task(task_id):
    from task_manager import get_task, mark_task_cancelled

    task = get_task(task_id)
    if not task:
        return jsonify({"error": "task not found"}), 404
    if task.get("status") not in ("starting", "running"):
        return jsonify({"error": f"task is {task.get('status')}, cannot cancel"}), 409

    pid = task.get("worker_pid")
    killed = False
    kill_errors = []
    candidate_pids = []
    if pid:
        candidate_pids.append(int(pid))
    else:
        try:
            finder = subprocess.run(
                ["pgrep", "-f", f"comment_fetcher.py.*--task-id {task_id}"],
                cwd=MEDIACRAWLER_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            candidate_pids.extend(int(p.strip()) for p in finder.stdout.splitlines() if p.strip())
        except Exception as exc:
            kill_errors.append(str(exc))

    for candidate_pid in dict.fromkeys(candidate_pids):
        try:
            os.killpg(candidate_pid, 15)
            killed = True
        except ProcessLookupError:
            killed = True
        except Exception as exc:
            kill_errors.append(f"{candidate_pid}: {exc}")

    mark_task_cancelled(task_id)
    return jsonify({"ok": True, "killed": killed, "errors": kill_errors})


@app.route("/api/tasks/<task_id>/log")
def api_get_task_log(task_id):
    from task_manager import get_task

    task = get_task(task_id)
    if not task:
        return jsonify({"error": "task not found", "lines": []}), 404
    log_path = task.get("log_path")
    if not log_path or not os.path.exists(log_path):
        return jsonify({"lines": [], "total": 0, "log_path": log_path})
    lines = request.args.get("lines", 120, type=int)
    lines = max(1, min(lines, 500))
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.read().splitlines()
    except Exception as exc:
        return jsonify({"error": str(exc), "lines": [], "log_path": log_path}), 500
    return jsonify({
        "lines": all_lines[-lines:],
        "total": len(all_lines),
        "log_path": log_path,
    })


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
