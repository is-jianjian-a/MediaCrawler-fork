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
import re
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
from pathlib import Path

from flask import Flask, has_request_context, jsonify, request

from db import (
    get_crawler_db_path, get_config_values, get_crawler_stats,
    get_velocity, get_latest_note, _connect, _kw_placeholders,
)
from groups import list_groups, save_group, activate_group, delete_group, rename_group, copy_group
from task_manager import init_task_db
from crawl_task_manager import init_crawl_task_db
try:
    from dashboard.account_registry import (
        AccountRegistryError,
        DEFAULT_ACCOUNT_ID,
        DEFAULT_USER_DATA_DIR,
        account_db_path,
        bind_task_config,
        get_account,
        init_account_registry_db,
        list_accounts,
    )
except ModuleNotFoundError:  # Support `python dashboard/server.py`.
    from account_registry import (  # type: ignore[no-redef]
        AccountRegistryError,
        DEFAULT_ACCOUNT_ID,
        DEFAULT_USER_DATA_DIR,
        account_db_path,
        bind_task_config,
        get_account,
        init_account_registry_db,
        list_accounts,
    )
try:
    from dashboard.risk_policy import (
        abort_launch,
        get_status as get_risk_policy_status,
        init_risk_policy_db,
        record_completion,
        reserve_launch,
    )
except ModuleNotFoundError:  # Support `python dashboard/server.py`.
    from risk_policy import (  # type: ignore[no-redef]
        abort_launch,
        get_status as get_risk_policy_status,
        init_risk_policy_db,
        record_completion,
        reserve_launch,
    )
try:
    from dashboard.profile_lock import native_profile_owner
except ModuleNotFoundError:  # Support `python dashboard/server.py`.
    from profile_lock import native_profile_owner  # type: ignore[no-redef]
from worth_scoring import score_post
from comment_fetcher import (
    DEFAULT_COMMENT_PUBLISH_DATE_AFTER,
    validate_comment_publish_date_after,
    validate_standard_browser_path,
)

try:
    from dashboard.rate_policy import (
        COMMENTS_RATE,
        DEFAULT_COMMENT_TASK_BATCH_DELAY,
        DEFAULT_COMMENT_TASK_BATCH_SIZE,
        DEFAULT_FIRST_LEVEL_COMMENTS,
        keyword_rate_defaults,
    )
except ModuleNotFoundError:  # Support `python dashboard/server.py`.
    from rate_policy import (  # type: ignore[no-redef]
        COMMENTS_RATE,
        DEFAULT_COMMENT_TASK_BATCH_DELAY,
        DEFAULT_COMMENT_TASK_BATCH_SIZE,
        DEFAULT_FIRST_LEVEL_COMMENTS,
        keyword_rate_defaults,
    )

# --- config ---
PORT = 18998
HOST = os.getenv("MEDIACRAWLER_DASHBOARD_HOST", "127.0.0.1")
MEDIACRAWLER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(MEDIACRAWLER_ROOT, "logs", "crawler.log")

DASHBOARD_DIR = os.path.join(MEDIACRAWLER_ROOT, "dashboard")
DASHBOARD_DB = os.path.join(DASHBOARD_DIR, "database", "dashboard.db")
STATIC_DIR = os.path.join(DASHBOARD_DIR, "static")

SNAPSHOT_INTERVAL = 30
RISK_SCHEDULER_INTERVAL = int(os.getenv("MEDIACRAWLER_RISK_SCHEDULER_INTERVAL", "30"))
RISK_SCHEDULER_ENABLED = os.getenv("MEDIACRAWLER_RISK_SCHEDULER_ENABLED", "true").lower() in ("1", "true", "yes")
MAX_PARALLEL_XHS_ACCOUNTS = max(
    1, int(os.getenv("MEDIACRAWLER_MAX_PARALLEL_XHS_ACCOUNTS", "2"))
)
MAX_DB_SIZE_MB = 100
MAX_HISTORY_HOURS = 72
MIN_COMMENT_TASK_BATCH_SIZE = int(os.getenv("MEDIACRAWLER_MIN_COMMENT_TASK_BATCH_SIZE", "5"))

logging.basicConfig(level=logging.INFO, format="[dashboard] %(levelname)s %(message)s")
logger = logging.getLogger("dashboard")

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
init_task_db()
init_crawl_task_db()
init_account_registry_db()
init_risk_policy_db()


def _cdp_debug_port() -> int:
    try:
        return int(os.getenv("MEDIACRAWLER_CDP_DEBUG_PORT", "9222"))
    except ValueError:
        return 9222


def _is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _cdp_websocket_from_active_port(port: int):
    candidates = [
        Path.home() / "Library/Application Support/Google/Chrome/DevToolsActivePort",
        Path.home() / "Library/Application Support/Google/Chrome/Default/DevToolsActivePort",
        Path(MEDIACRAWLER_ROOT) / "browser_data" / "chrome-cdp-debug" / "DevToolsActivePort",
        Path(MEDIACRAWLER_ROOT) / "browser_data" / f"chrome-cdp-debug-{port}" / "DevToolsActivePort",
    ]
    for candidate in candidates:
        try:
            if not candidate.exists():
                continue
            lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) < 2:
                continue
            active_port = int(lines[0].strip())
            browser_path = lines[1].strip()
            if active_port == port and browser_path.startswith("/devtools/browser/"):
                return f"ws://127.0.0.1:{active_port}{browser_path}"
        except Exception:
            logger.exception(f"Unhandled exception in _cdp_websocket_from_active_port()")
            continue
    return ""


async def _verify_playwright_cdp(endpoint: str, timeout: float = 3.0):
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            await playwright.chromium.connect_over_cdp(
                endpoint,
                timeout=timeout * 1000,
            )
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _check_playwright_cdp(endpoint: str, timeout: float = 3.0):
    try:
        return asyncio.run(_verify_playwright_cdp(endpoint, max(timeout, 10.0)))
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
    playwright_chromium = ""
    try:
        # Prefer Playwright's isolated Chromium.  The crawler must not launch the
        # user's daily Chrome just because no explicit browser path was supplied.
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            candidate = playwright.chromium.executable_path
            if candidate and os.path.exists(candidate):
                playwright_chromium = candidate
    except Exception as exc:
        logger.warning("Unable to locate Playwright Chromium: %s", exc)
    candidates = [
        configured,
        playwright_chromium,
        "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        shutil.which("google-chrome") or "",
        shutil.which("chromium") or "",
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    raise RuntimeError(
        "Isolated Chromium/Chrome for Testing not found. Set MEDIACRAWLER_BROWSER_PATH "
        "to a crawler-only browser; the user's daily Chrome will not be launched automatically."
    )


def check_cdp_remote_debugging(timeout: float = 2.0, port: int = None, verify_playwright: bool = False):
    port = port or _cdp_debug_port()
    url = f"http://127.0.0.1:{port}/json/version"
    data = {}
    browser = ""
    web_socket = ""
    source = "json/version"
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
        web_socket = _cdp_websocket_from_active_port(port)
        if not web_socket:
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
        source = "DevToolsActivePort"
    except Exception as exc:
        return {"ok": False, "port": port, "url": url, "error": str(exc)}
    if data:
        browser = data.get("Browser", "")
        web_socket = data.get("webSocketDebuggerUrl", "")
    ok = bool(browser or web_socket)
    result = {
        "ok": ok,
        "port": port,
        "url": url,
        "browser": browser,
        "webSocketDebuggerUrl": web_socket,
        "source": source,
        "raw": data,
    }
    if verify_playwright and ok:
        endpoint = web_socket or f"http://127.0.0.1:{port}"
        playwright_ok, playwright_error = _check_playwright_cdp(endpoint, timeout=max(timeout, 10.0))
        result["playwright_ok"] = playwright_ok
        if not playwright_ok:
            result["ok"] = False
            result["error"] = playwright_error
            result["hint"] = (
                "CDP endpoint is reachable, but Playwright cannot connect to it. "
                "Use the Dashboard CDP Chrome launcher to start a compatible remote-debugging browser."
            )
    return result


def start_cdp_chrome(preferred_port: int = None):
    start_port = preferred_port or _cdp_debug_port()
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
        "--remote-debugging-address=127.0.0.1",
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
        "--disable-blink-features=AutomationControlled",
        "--exclude-switches=enable-automation",
        "--disable-infobars",
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


def _with_crawler_db(account_id: str = ""):
    """Return an account-routed content DB connection, or None when absent."""
    selected_account = str(account_id or "").strip()
    if not selected_account and has_request_context():
        selected_account = str(request.args.get("account_id", "") or "").strip()
    if not selected_account:
        # Backward-compatible default route.  It also keeps read-only tests and
        # local tools that patch the legacy DB pointer working unchanged.
        db_path = _crawler_db_path
    else:
        try:
            db_path = account_db_path(selected_account)
        except AccountRegistryError:
            return None
    if not db_path or not os.path.exists(db_path):
        return None
    return _connect(db_path)


def ensure_crawler_db_indexes():
    """Create lightweight indexes needed by dashboard read queries."""
    for account in list_accounts():
        conn = _with_crawler_db(account["account_id"])
        if not conn:
            continue
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_xhs_note_comment_note_id ON xhs_note_comment(note_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_xhs_note_source_keyword ON xhs_note(source_keyword)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_xhs_note_add_ts ON xhs_note(add_ts)")
            has_keyword_hits = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='xhs_note_keyword_hit'"
            ).fetchone()
            if has_keyword_hits:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_xhs_note_keyword_hit_keyword_note "
                    "ON xhs_note_keyword_hit(keyword, note_id)"
                )
            conn.commit()
        finally:
            conn.close()


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
            logger.exception(f"Unhandled exception in get_process_info()")
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
        logger.exception(f"Unhandled exception in get_process_info()")
        return False, "", 0.0, 0, "unknown"


# --- snapshot DB (dashboard's own history storage) ---

def get_db_size_mb():
    if os.path.exists(DASHBOARD_DB):
        return os.path.getsize(DASHBOARD_DB) / (1024 * 1024)
    return 0


def get_crawl_task_health():
    """Return task-aware crawler health for dashboard status labels.

    The dashboard used to mark a crawler as failed solely from "minutes since
    last DB write". That is wrong when a dashboard-created crawl task has
    already completed successfully. Task state is the authoritative source
    here; write gap is only a liveness signal while a task is running.
    """
    try:
        from crawl_task_manager import list_crawl_tasks
        tasks = list_crawl_tasks(archived=False)
    except Exception:
        logger.exception(f"Unhandled exception in get_crawl_task_health()")
        return {
            "status": "unknown",
            "label": "状态未知",
            "color": "warning",
            "active_task": None,
            "recent_task": None,
        }

    active = next((t for t in tasks if t.get("status") in ("starting", "running", "stopping")), None)
    recent = tasks[0] if tasks else None
    task = active or recent
    if not task:
        return {
            "status": "idle",
            "label": "空闲",
            "color": "ok",
            "active_task": None,
            "recent_task": None,
        }

    status = task.get("status")
    if active:
        return {
            "status": status,
            "label": {"running": "运行中", "starting": "启动中", "stopping": "停止中"}.get(status, status),
            "color": "ok" if status == "running" else "warning",
            "active_task": active,
            "recent_task": recent,
        }
    if status in ("completed", "failed", "pending", "cancelled"):
        return {
            "status": "idle",
            "label": "当前无运行任务",
            "color": "ok",
            "active_task": None,
            "recent_task": recent,
        }
    return {
        "status": status or "unknown",
        "label": status or "状态未知",
        "color": "warning",
        "active_task": None,
        "recent_task": recent,
    }


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
        logger.exception(f"Unhandled exception in take_snapshot()")
        try:
            conn2.close()
        except Exception:
            logger.exception(f"Unhandled exception in take_snapshot()")
            pass


def snapshot_loop():
    while True:
        time.sleep(SNAPSHOT_INTERVAL)
        try:
            take_snapshot()
        except Exception as e:
            logger.error(f"Snapshot failed: {e}")


# --- index ---


@app.route("/")
def index():
    html_path = os.path.join(STATIC_DIR, "dashboard.html")
    if not os.path.exists(html_path):
        return "dashboard.html not found", 404
    with open(html_path, "r") as f:
        html = f.read()
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

    health_ts = max(stats.get("last_crawl_ts", 0) or 0, stats.get("global_last_crawl_ts", 0) or 0)
    last_write_gap = (time.time() * 1000 - health_ts) / 1000 if health_ts else None
    task_health = get_crawl_task_health()
    active_task = task_health.get("active_task")
    recent_task = task_health.get("recent_task")

    # Overall verdict: task status first; write gap only matters while a task is active.
    verdict = task_health["label"]
    vcolor = task_health["color"]
    if active_task:
        worker_pid = active_task.get("worker_pid")
        worker_alive = _pid_alive(worker_pid) if worker_pid else alive
        if active_task.get("status") == "running" and not worker_alive:
            verdict, vcolor = "任务进程丢失", "error"
        elif not health_ts:
            verdict, vcolor = "启动中，等待首次写入", "warning"
        elif last_write_gap is not None and last_write_gap >= 1800:
            verdict, vcolor = f"运行停滞（{last_write_gap/60:.0f}分钟无写入）", "error"
        elif last_write_gap is not None and last_write_gap >= 600:
            verdict, vcolor = f"运行中（{last_write_gap/60:.0f}分钟无写入）", "warning"
    elif recent_task:
        verdict, vcolor = "当前无运行任务", "ok"

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
        "health": {
            "status": task_health["status"],
            "last_write_seconds_ago": last_write_gap,
            "active_task_id": active_task.get("id") if active_task else None,
            "recent_task_id": recent_task.get("id") if recent_task else None,
            "recent_task_status": recent_task.get("status") if recent_task else None,
        },
        "process": {"alive": alive, "uptime": uptime, "cpu": cpu, "rss_mb": rss_mb, "platform": platform},
        "stats": stats,
        "keywords": keywords,
        "keywords_table": keywords_table,
        "max_notes_per_keyword": max_notes,
        "db_size_mb": round(db_size, 1),
        "db_size_warning": db_size > MAX_DB_SIZE_MB,
    })


def _asset_scope_rows(conn, keywords):
    """Return one canonical note row plus every selected keyword hit.

    `source_keyword` is retained for legacy notes while
    `xhs_note_keyword_hit` records newer multi-keyword provenance.  The union is
    the Dashboard's dataset boundary, so a note is counted once even when it
    was found by several selected keywords.
    """
    if not keywords:
        return [], {}
    ph = _kw_placeholders(keywords)
    cur = conn.cursor()
    has_keyword_hits = cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='xhs_note_keyword_hit'"
    ).fetchone()
    hit_union = (
        f"UNION SELECT note_id, keyword FROM xhs_note_keyword_hit WHERE keyword IN ({ph})"
        if has_keyword_hits else ""
    )
    params = keywords + keywords if has_keyword_hits else keywords
    scope_cte = f"""
        WITH scoped_hits AS (
            SELECT note_id, source_keyword AS keyword
            FROM xhs_note
            WHERE source_keyword IN ({ph}) AND TRIM(source_keyword) <> ''
            {hit_union}
        ),
        scoped_ids AS (
            SELECT note_id, COUNT(DISTINCT keyword) AS matched_keyword_count
            FROM scoped_hits GROUP BY note_id
        ),
        comment_counts AS (
            SELECT c.note_id, COUNT(*) AS db_comment_count
            FROM xhs_note_comment c
            JOIN scoped_ids s ON s.note_id = c.note_id
            GROUP BY c.note_id
        )
    """
    cur.execute(
        scope_cte + """
        SELECT n.note_id, n.title, n.desc, n.source_keyword,
               COALESCE(n.liked_count, 0), COALESCE(n.collected_count, 0),
               COALESCE(n.comment_count, 0), COALESCE(n.add_ts, 0),
               COALESCE(n.time, 0), COALESCE(c.db_comment_count, 0),
               s.matched_keyword_count
        FROM scoped_ids s
        JOIN xhs_note n ON n.note_id = s.note_id
        LEFT JOIN comment_counts c ON c.note_id = n.note_id
        """,
        params,
    )
    rows = cur.fetchall()
    cur.execute(
        f"""
        WITH scoped_hits AS (
            SELECT note_id, source_keyword AS keyword
            FROM xhs_note
            WHERE source_keyword IN ({ph}) AND TRIM(source_keyword) <> ''
            {hit_union}
        )
        SELECT note_id, keyword FROM scoped_hits
        """,
        params,
    )
    note_keywords = {}
    for row in cur.fetchall():
        note_keywords.setdefault(row[0], []).append(row[1])
    return rows, note_keywords


@app.route("/api/asset-summary")
def api_asset_summary():
    """Analysis-readiness and collection-gap summary for the active group."""
    keywords, _ = get_config_values()
    conn = _with_crawler_db()
    if not conn:
        return jsonify({"error": "no DB connection"}), 503
    try:
        rows, note_keywords = _asset_scope_rows(conn, keywords)
        total_posts = len(rows)
        total_comments = sum(int(row[9] or 0) for row in rows)
        posts_with_comments = sum(1 for row in rows if int(row[9] or 0) > 0)
        empty_desc = sum(1 for row in rows if not str(row[2] or "").strip())
        text_empty = sum(
            1 for row in rows
            if not (str(row[1] or "").strip() or str(row[2] or "").strip())
        )
        analysis_ready = 0
        comment_gap_candidates = 0
        scored = []
        for row in rows:
            note_id = row[0]
            title = str(row[1] or "")
            desc = str(row[2] or "")
            liked = int(row[4] or 0)
            collected = int(row[5] or 0)
            platform_comments = int(row[6] or 0)
            db_comments = int(row[9] or 0)
            has_text = bool(title.strip() or desc.strip())
            if has_text and (db_comments > 0 or platform_comments > 0 or liked + collected >= 10):
                analysis_ready += 1
            if platform_comments >= 10 and db_comments < min(platform_comments, 10):
                comment_gap_candidates += 1
            post = {
                "note_id": note_id,
                "title": title,
                "source_keyword": row[3] or "",
                "liked_count": liked,
                "comment_count": platform_comments,
                "db_comment_count": db_comments,
                "desc_length": len(desc),
            }
            post.update(score_post(post))
            scored.append({
                **post,
                "keywords": note_keywords.get(note_id, []),
                "add_ts": int(row[7] or 0),
            })

        scored.sort(
            key=lambda item: (
                item.get("worth_score", 0),
                item.get("comment_gap", 0),
                item.get("liked_count", 0),
            ),
            reverse=True,
        )
        high_value_count = sum(1 for item in scored if item.get("worth_score", 0) >= 60)
        multi_keyword_posts = sum(1 for row in rows if int(row[10] or 0) >= 2)
        publish_times = [int(row[8] or 0) for row in rows if int(row[8] or 0) > 0]
        latest_row = max(rows, key=lambda row: int(row[7] or 0), default=None)

        by_note = {row[0]: row for row in rows}
        coverage = []
        for keyword in keywords:
            note_ids = [note_id for note_id, hits in note_keywords.items() if keyword in hits]
            keyword_rows = [by_note[note_id] for note_id in note_ids if note_id in by_note]
            with_comments = sum(1 for row in keyword_rows if int(row[9] or 0) > 0)
            gap_count = sum(
                1 for row in keyword_rows
                if int(row[6] or 0) >= 10 and int(row[9] or 0) < min(int(row[6] or 0), 10)
            )
            post_count = len(keyword_rows)
            if post_count < 10:
                status = "样本偏少"
            elif gap_count:
                status = "值得补评"
            elif with_comments / max(1, post_count) >= 0.4:
                status = "可分析"
            else:
                status = "评论偏少"
            coverage.append({
                "keyword": keyword,
                "post_count": post_count,
                "db_comment_count": sum(int(row[9] or 0) for row in keyword_rows),
                "posts_with_comments": with_comments,
                "comment_coverage_pct": round(with_comments / max(1, post_count) * 100),
                "comment_gap_candidates": gap_count,
                "latest_add_ts": max((int(row[7] or 0) for row in keyword_rows), default=0),
                "status": status,
            })

        return jsonify({
            "keywords": keywords,
            "total_posts": total_posts,
            "total_comments": total_comments,
            "analysis_ready_posts": analysis_ready,
            "analysis_ready_pct": round(analysis_ready / max(1, total_posts) * 100),
            "posts_with_comments": posts_with_comments,
            "comment_coverage_pct": round(posts_with_comments / max(1, total_posts) * 100),
            "zero_comment_posts": total_posts - posts_with_comments,
            "comment_gap_candidates": comment_gap_candidates,
            "high_value_posts": high_value_count,
            "multi_keyword_posts": multi_keyword_posts,
            "empty_desc_count": empty_desc,
            "empty_desc_pct": round(empty_desc / max(1, total_posts) * 100, 1),
            "text_empty_count": text_empty,
            "publish_date_min": min(publish_times, default=0),
            "publish_date_max": max(publish_times, default=0),
            "latest_ingest_ts": int(latest_row[7] or 0) if latest_row else 0,
            "latest_ingest_keyword": (latest_row[3] or "") if latest_row else "",
            "keyword_coverage": coverage,
            "top_opportunities": scored[:8],
        })
    finally:
        conn.close()


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
        logger.exception(f"Unhandled exception in api_history()")
        return jsonify([])


@app.route("/api/velocity")
def api_velocity():
    keywords, _ = get_config_values()
    conn = _with_crawler_db()
    if not conn:
        return jsonify({"minute": {"data": [], "speed": {"posts": 0, "comments": 0}},
                         "hour": {"data": [], "speed": {"posts": 0, "comments": 0}}})
    try:
        # 支持自定义分钟数，默认180（3小时）
        minutes = request.args.get("minutes", 180, type=int)
        minutes = max(1, min(minutes, 1440))  # clamp 1-1440
        
        minute = get_velocity(conn, keywords, "minute", minutes)
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
            cur.execute(
                """
                SELECT MAX(ts) FROM (
                    SELECT MAX(add_ts) AS ts FROM xhs_note
                    UNION ALL
                    SELECT MAX(add_ts) AS ts FROM xhs_note_comment
                )
                """
            )
            row = cur.fetchone()
            ts = row[0] if row and row[0] else 0
            if ts:
                last_write = round((time.time() * 1000 - ts) / 1000, 1)
        finally:
            conn.close()
    alive, _, _, _, platform = get_process_info()
    task_health = get_crawl_task_health()
    active_task = task_health.get("active_task")
    recent_task = task_health.get("recent_task")

    status = task_health["status"]
    if not db_ok:
        status = "db_disconnected"
    elif active_task:
        if active_task.get("status") == "running" and active_task.get("worker_pid") and not _pid_alive(active_task.get("worker_pid")):
            status = "worker_lost"
        elif last_write and last_write > 1800:
            status = "stalled"
        elif last_write and last_write > 600:
            status = "slow"
    elif not recent_task:
        status = "idle"

    return jsonify({
        "status": status,
        "label": task_health["label"],
        "db_connected": db_ok,
        "crawler_alive": alive,
        "crawler_platform": platform,
        "last_write_seconds_ago": last_write,
        "active_task_id": active_task.get("id") if active_task else None,
        "recent_task_id": recent_task.get("id") if recent_task else None,
        "recent_task_status": recent_task.get("status") if recent_task else None,
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
    account_id = str(request.args.get("account_id", "") or "").strip()
    return jsonify(list_tasks(archived=archived, account_id=account_id))


@app.route("/api/xhs-accounts")
def api_list_xhs_accounts():
    """Expose only non-sensitive routing state to the loopback Dashboard."""
    accounts = []
    for account in list_accounts(include_disabled=True):
        accounts.append(
            {
                "account_id": account["account_id"],
                "display_name": account["display_name"],
                "enabled": account["enabled"],
                "storage_mode": account["storage_mode"],
                "profile_exists": account["profile_exists"],
                "content_db_exists": account["content_db_exists"],
                "profile_in_use": bool(native_profile_owner(account["user_data_dir"])),
                "risk_policy": get_risk_policy_status(account["user_data_dir"]),
            }
        )
    return jsonify({"accounts": accounts, "default_account_id": DEFAULT_ACCOUNT_ID})


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
    raw_config = data.get("config", {}) or {}
    selected_account_id = str(
        data.get("account_id")
        or raw_config.get("account_id")
        or DEFAULT_ACCOUNT_ID
    ).strip()
    group_tag = (data.get("group_tag") or "").strip()[:40]
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
    if len(posts) > 2:
        return jsonify({"error": "risk policy allows at most 2 posts per comment task"}), 400
    try:
        browser_mode = str(raw_config.get("browser_mode", "standard") or "standard")
        if browser_mode not in ("standard", "cdp_optional", "cdp_required"):
            browser_mode = "standard"
        min_sleep = max(0, min(int(raw_config.get("min_sleep", COMMENTS_RATE.min_sleep)), 300))
        max_sleep = max(0, min(int(raw_config.get("max_sleep", COMMENTS_RATE.max_sleep)), 600))
        if max_sleep < min_sleep:
            raise ValueError("max_sleep must not be lower than min_sleep")
        config = {
            "batch_size": max(1, min(int(raw_config.get("batch_size", DEFAULT_COMMENT_TASK_BATCH_SIZE)), 20)),
            "max_comments": max(1, min(int(raw_config.get("max_comments", DEFAULT_FIRST_LEVEL_COMMENTS)), 5)),
            "max_sub_comments": max(0, min(int(raw_config.get("max_sub_comments", 200)), 1000)),
            "get_sub_comments": raw_config.get("get_sub_comments", False) is True,
            "delay": max(0, min(float(raw_config.get("delay", DEFAULT_COMMENT_TASK_BATCH_DELAY)), 120)),
            "min_sleep": min_sleep,
            "max_sleep": max_sleep,
            "comment_sleep": max(90, min(int(raw_config.get("comment_sleep", COMMENTS_RATE.comment_sleep)), 120)),
            "max_concurrency": 1,
            "limit": max(0, min(int(raw_config.get("limit", 0) or 0), 100)),
            "dry_run": raw_config.get("dry_run", False) is True,
            "headless": True,
            "browser_mode": browser_mode,
            "enable_cdp": False,
            "require_cdp": False,
            "cdp_debug_port": max(1, min(int(raw_config.get("cdp_debug_port", 9222) or 9222), 65535)),
            "publish_date_after": validate_comment_publish_date_after(
                raw_config.get("publish_date_after", DEFAULT_COMMENT_PUBLISH_DATE_AFTER)
            ),
            "start_mode": "auto" if raw_config.get("start_mode") == "auto" else "manual",
        }
        config, account = bind_task_config(config, account_id=selected_account_id)
        account_conn = _with_crawler_db(account["account_id"])
        if not account_conn:
            return jsonify({"error": "account content database is not initialized"}), 409
        try:
            placeholders = ",".join("?" for _ in posts)
            query_params = [post["note_id"] for post in posts]
            existing_note_ids = {
                row[0]
                for row in account_conn.execute(
                    f"SELECT note_id FROM xhs_note WHERE note_id IN ({placeholders})",
                    query_params,
                ).fetchall()
            }
        finally:
            account_conn.close()
        missing = [
            post["note_id"]
            for post in posts
            if post["note_id"] not in existing_note_ids
        ]
        if missing:
            return jsonify(
                {
                    "error": "posts do not belong to selected account",
                    "missing_note_ids": missing,
                }
            ), 400
    except (AccountRegistryError, TypeError, ValueError) as exc:
        return jsonify({"error": f"invalid task config: {exc}"}), 400
    task_id = create_task(
        name,
        posts,
        config,
        group_tag=group_tag,
        account_id=account["account_id"],
    )
    return jsonify({"id": task_id, "ok": True})


def _runtime_account_config(task: dict, config: dict) -> tuple[dict, dict]:
    account_id = str(
        task.get("account_id") or config.get("account_id") or DEFAULT_ACCOUNT_ID
    ).strip()
    return bind_task_config(config, account_id=account_id, require_enabled=True)


def _account_worker_log_path(account_id: str, task_id: str) -> str:
    log_dir = Path(DASHBOARD_DIR) / "logs" / "accounts" / account_id
    log_dir.mkdir(parents=True, exist_ok=True)
    return str(log_dir / f"{task_id}-worker.log")


def _record_task_cancellation(task: dict, task_kind: str) -> None:
    """Release only this task's account lease after an explicit cancellation."""
    config = _auto_task_config(task)
    try:
        runtime_config, _ = bind_task_config(
            config,
            account_id=_auto_task_account_id(task),
            require_enabled=False,
        )
        record_completion(
            task_id=task["id"],
            task_kind=task_kind,
            user_data_dir=runtime_config["user_data_dir"],
            exit_code=130,
        )
    except Exception:
        logger.exception(
            "failed to record cancelled %s task lease: %s",
            task_kind,
            task.get("id"),
        )


def _account_has_active_task(account_id: str, *, exclude_task_id: str = "") -> bool:
    from crawl_task_manager import list_crawl_tasks
    from task_manager import list_tasks

    active_statuses = {"starting", "running", "stopping"}
    selected = get_account(account_id)
    if not selected:
        return True
    candidates = list_crawl_tasks() + list_tasks()
    for task in candidates:
        if task.get("id") == exclude_task_id or task.get("status") not in active_statuses:
            continue
        active_account_id = _auto_task_account_id(task)
        if active_account_id == account_id:
            return True
        active_account = get_account(active_account_id)
        if not active_account:
            continue
        if (
            active_account["profile_path"] == selected["profile_path"]
            or active_account["sqlite_db_path"] == selected["sqlite_db_path"]
        ):
            return True
    return False


def _profile_busy_response(account: dict):
    owner = native_profile_owner(account["user_data_dir"])
    if not owner:
        return None
    return jsonify(
        {
            "error": "selected account browser profile is already open",
            "profile_in_use": True,
            "owner_pid": owner.get("pid", 0),
            "hint": "Close the isolated login window before starting this task.",
        }
    ), 409


def _launch_comment_task(task_id, retry_failed=False):
    from task_manager import claim_task, fail_task_start, get_task, set_task_worker_pid

    task = get_task(task_id)
    if not task:
        return jsonify({"error": "task not found"}), 404
    try:
        task_config = json.loads(task.get("config_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        task_config = {}
    try:
        task_config, account = _runtime_account_config(task, task_config)
    except AccountRegistryError as exc:
        return jsonify({"error": str(exc)}), 409
    if _account_has_active_task(account["account_id"], exclude_task_id=task_id):
        return jsonify({"error": "selected account already has an active task"}), 409
    profile_busy = _profile_busy_response(account)
    if profile_busy:
        return profile_busy
    dry_run = bool(task_config.get("dry_run"))
    if not dry_run:
        decision = reserve_launch(
            task_id=task_id,
            task_kind="comment",
            config=task_config,
            total_posts=int(task.get("total_posts", 0) or 0),
        )
        if not decision.allowed:
            return jsonify({"error": decision.reason, "risk_policy": decision.as_dict()}), 429

    claimed, error = claim_task(task_id, retry_failed=retry_failed)
    if not claimed:
        if not dry_run:
            abort_launch(task_id, task_config.get("user_data_dir", ""), error)
        status = 404 if error == "task not found" else 409
        return jsonify({"error": error}), status
    effective_batch_size = max(
        1,
        min(
            int(task_config.get("batch_size", DEFAULT_COMMENT_TASK_BATCH_SIZE) or DEFAULT_COMMENT_TASK_BATCH_SIZE),
            20,
        ),
    )
    try:
        publish_date_after = validate_comment_publish_date_after(
            task_config.get("publish_date_after", DEFAULT_COMMENT_PUBLISH_DATE_AFTER)
        )
    except ValueError as exc:
        fail_task_start(task_id, str(exc))
        if not dry_run:
            abort_launch(task_id, task_config.get("user_data_dir", ""), str(exc))
        return jsonify({"error": f"failed to start worker: {exc}"}), 500

    uv = shutil.which("uv")
    command = ([uv, "run", "python"] if uv else [sys.executable]) + [
        os.path.join(DASHBOARD_DIR, "comment_fetcher.py"),
        "--task-id", task_id,
        "--batch-size", str(effective_batch_size),
        "--max-comments", str(task_config.get("max_comments", DEFAULT_FIRST_LEVEL_COMMENTS)),
        "--max-sub-comments", str(task_config.get("max_sub_comments", 200)),
        "--delay", str(task_config.get("delay", DEFAULT_COMMENT_TASK_BATCH_DELAY)),
        "--min-sleep", str(task_config.get("min_sleep", COMMENTS_RATE.min_sleep)),
        "--max-sleep", str(task_config.get("max_sleep", COMMENTS_RATE.max_sleep)),
        "--comment-sleep", str(task_config.get("comment_sleep", COMMENTS_RATE.comment_sleep)),
        "--max-concurrency", "1",
        "--account-id", account["account_id"],
        "--db-path", account["sqlite_db_path"],
        "--user-data-dir", account["user_data_dir"],
        "--publish-date-after", publish_date_after,
    ]
    if int(task_config.get("limit", 0) or 0) > 0:
        command.extend(["--limit", str(task_config.get("limit"))])
    if task_config.get("dry_run"):
        command.append("--dry-run")
    if retry_failed:
        command.append("--retry-failed")
    if task_config.get("get_sub_comments"):
        command.append("--get-sub-comments")

    try:
        worker_env = os.environ.copy()
        enable_cdp = bool(task_config.get("enable_cdp"))
        require_cdp = bool(task_config.get("require_cdp"))
        if enable_cdp or require_cdp:
            raise ValueError(
                "comment tasks require standard isolated browser mode; CDP is disabled"
            )
        if not enable_cdp:
            browser_path = validate_standard_browser_path(
                task_config.get("browser_path", "")
            )
            command.extend(["--browser-path", browser_path])
            worker_env["MEDIACRAWLER_BROWSER_PATH"] = browser_path
        if enable_cdp:
            cdp_status = start_cdp_chrome()
            if not cdp_status.get("ok") and require_cdp:
                fail_task_start(task_id, cdp_status.get("error") or "CDP Chrome unavailable")
                return jsonify({"error": cdp_status}), 500
        worker_env["MEDIACRAWLER_ENABLE_CDP"] = "true" if enable_cdp else "false"
        worker_env["MEDIACRAWLER_REQUIRE_CDP"] = "true" if require_cdp else "false"
        worker_env["MEDIACRAWLER_CDP_DEBUG_PORT"] = str(task_config.get("cdp_debug_port", 9222))
        worker_env["MEDIACRAWLER_AUTO_CLOSE_BROWSER"] = "true"
        worker_env["MEDIACRAWLER_CDP_CONNECT_EXISTING"] = "false"
        worker_env.pop("MEDIACRAWLER_CDP_ENDPOINT", None)
        worker_env["MEDIACRAWLER_XHS_NOTE_PUBLISH_DATE_AFTER"] = publish_date_after
        worker_env["MEDIACRAWLER_ACCOUNT"] = account["account_id"]
        worker_env["MEDIACRAWLER_SQLITE_DB_PATH"] = account["sqlite_db_path"]
        worker_env["MEDIACRAWLER_USER_DATA_DIR"] = account["user_data_dir"]
        worker_env["MEDIACRAWLER_TASK_ID"] = task_id
        worker_env["MEDIACRAWLER_LOG_PATH"] = _account_worker_log_path(
            account["account_id"], task_id
        )
        worker = subprocess.Popen(
            command,
            cwd=MEDIACRAWLER_ROOT,
            env=worker_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        set_task_worker_pid(task_id, worker.pid)
    except Exception as exc:
        fail_task_start(task_id, str(exc))
        if not dry_run:
            abort_launch(task_id, task_config.get("user_data_dir", ""), str(exc))
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
    _record_task_cancellation(task, "comment")
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


# --- keyword crawl task API ---

CRAWL_KEYWORD_PRESETS = [
    {
        "name": "流畅/卡顿首轮",
        "desc": "围绕手机体验里的流畅、卡顿、丝滑、稳定和性能感知，适合从新到旧抓取。",
        "keywords": [
            "手机卡顿", "手机流畅", "手机丝滑", "手机稳定", "手机性能",
            "手机越用越卡", "手机不流畅", "手机反应慢", "系统卡顿", "系统流畅",
            "安卓卡顿", "苹果卡顿", "华为卡顿", "小米卡顿", "苹果流畅", "华为流畅",
        ],
    },
    {
        "name": "品牌对比",
        "desc": "覆盖苹果、华为、小米、安卓等品牌和系统体验对比。",
        "keywords": [
            "华为和苹果卡顿对比", "华为和苹果性能对比", "华为和苹果流畅度对比",
            "华为和苹果稳定性对比", "华为和苹果丝滑对比", "安卓和苹果卡顿",
            "安卓和苹果流畅度", "小米和苹果流畅度", "华为和小米流畅度",
        ],
    },
    {
        "name": "卡顿问题",
        "desc": "偏问题表达，用来捕捉真实抱怨和卡顿触发场景。",
        "keywords": [
            "手机卡顿怎么办", "手机突然卡顿", "手机掉帧", "手机发热卡顿",
            "手机更新后卡顿", "手机软件卡顿", "手机打字卡顿", "手机滑动卡顿",
            "手机相机卡顿", "手机微信卡顿",
        ],
    },
    {
        "name": "流畅体验",
        "desc": "偏正向表达，用来捕捉用户对流畅、丝滑、稳定的描述。",
        "keywords": [
            "手机很流畅", "系统很流畅", "手机丝滑体验", "手机动画丝滑",
            "手机用起来丝滑", "手机稳定流畅", "手机不卡顿", "流畅度提升",
            "系统流畅度", "手机顺滑",
        ],
    },
    {
        "name": "性能感知",
        "desc": "覆盖性能、内存、刷新率、系统更新等可能影响流畅感的因素。",
        "keywords": [
            "手机性能体验", "手机内存不够卡", "手机刷新率流畅", "手机高刷流畅",
            "手机系统优化", "手机系统更新体验", "手机后台卡顿", "手机应用启动慢",
        ],
    },
]


def _normalize_crawl_keywords(raw_keywords):
    if isinstance(raw_keywords, str):
        parts = raw_keywords.replace("\n", ",").replace("，", ",").split(",")
    elif isinstance(raw_keywords, list):
        parts = raw_keywords
    else:
        parts = []
    keywords = []
    seen = set()
    for item in parts:
        kw = str(item or "").strip()
        if not kw or kw in seen:
            continue
        seen.add(kw)
        keywords.append(kw)
    return keywords


def _normalize_crawl_config(config):
    config = config or {}
    browser_mode = str(config.get("browser_mode", "standard") or "standard")
    if browser_mode not in ("standard", "cdp_optional", "cdp_required"):
        browser_mode = "cdp_optional"
    note_type = str(config.get("note_type", "all") or "all")
    if note_type not in ("all", "video", "image"):
        note_type = "all"
    sort_type = str(config.get("sort_type", "time_descending") or "time_descending")
    if sort_type not in ("general", "popularity_descending", "time_descending"):
        sort_type = "time_descending"
    count_mode = str(config.get("count_mode", "incremental") or "incremental")
    if count_mode not in ("incremental", "total"):
        count_mode = "incremental"
    stop_condition = str(config.get("stop_condition", "new_count") or "new_count")
    if stop_condition not in ("date_floor", "new_count"):
        stop_condition = "new_count"
    user_data_dir = str(config.get("user_data_dir", "%s_user_data_dir_account02") or "").strip() or "%s_user_data_dir_account02"
    publish_date_after = str(config.get("publish_date_after", "2026-06-10") or "").strip()
    default_max_count = 0 if stop_condition == "date_floor" else 100
    get_comments = config.get("get_comments", False) is True
    rate_defaults = keyword_rate_defaults(get_comments)
    start_page = int(config.get("start_page", 1))
    if not 1 <= start_page <= 1000:
        raise ValueError("start_page must be between 1 and 1000")
    min_sleep = max(0, min(int(config.get("min_sleep", rate_defaults["min_sleep"])), 300))
    max_sleep = max(0, min(int(config.get("max_sleep", rate_defaults["max_sleep"])), 600))
    if max_sleep < min_sleep:
        raise ValueError("max_sleep must not be lower than min_sleep")
    return {
        "topic": str(config.get("topic", "fluency_lag") or "fluency_lag"),
        "start_page": start_page,
        "stop_condition": stop_condition,
        "publish_date_after": publish_date_after,
        "max_count": max(0, min(int(config.get("max_count", default_max_count)), 200000)),
        "count_mode": count_mode,
        "sort_type": sort_type,
        "note_type": note_type,
        "get_comments": get_comments,
        "get_sub_comments": get_comments and config.get("get_sub_comments", False) is True,
        "max_comments": max(0, min(int(config.get("max_comments", DEFAULT_FIRST_LEVEL_COMMENTS)), 500)),
        "max_sub_comments": max(0, min(int(config.get("max_sub_comments", 10)), 1000)),
        "max_concurrency": max(1, min(int(config.get("max_concurrency", 1)), 5)),
        "enable_random_sleep": config.get("enable_random_sleep", True) is True,
        "min_sleep": min_sleep,
        "max_sleep": max_sleep,
        "comment_sleep": max(1, min(int(config.get("comment_sleep", rate_defaults["comment_sleep"])), 120)),
        "note_detail_timeout": max(10, min(int(config.get("note_detail_timeout", 75)), 300)),
        "headless": config.get("headless", True) is not False,
        "dry_run": config.get("dry_run", False) is True,
        "user_data_dir": user_data_dir,
        "browser_mode": browser_mode,
        "enable_cdp": False,
        "require_cdp": False,
        "cdp_debug_port": max(1, min(int(config.get("cdp_debug_port", 9222) or 9222), 65535)),
        "browser_path": str(config.get("browser_path", "") or "").strip(),
        "start_mode": "auto" if config.get("start_mode") == "auto" else "manual",
    }


@app.route("/api/crawl-keyword-presets")
def api_crawl_keyword_presets():
    return jsonify({"groups": CRAWL_KEYWORD_PRESETS})


@app.route("/api/crawl-tasks")
def api_list_crawl_tasks():
    from crawl_task_manager import list_crawl_tasks
    archived = request.args.get("archived") in ("1", "true", "yes")
    account_id = str(request.args.get("account_id", "") or "").strip()
    return jsonify(list_crawl_tasks(archived=archived, account_id=account_id))


def _pid_alive(pid) -> bool:
    try:
        if not pid:
            return False
        os.kill(int(pid), 0)
        return True
    except Exception:
        logger.exception(f"Unhandled exception in _pid_alive()")
        return False


def _read_tail_lines(log_path: str, limit: int = 80) -> tuple[list[str], int]:
    if not log_path or not os.path.exists(log_path):
        return [], 0
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        return lines[-limit:], len(lines)
    except Exception as exc:
        return [f"[dashboard-log-error] {exc}"], 1


def _parse_crawl_log(lines: list[str]) -> dict:
    info = {
        "current_keyword": "",
        "last_detail_note_id": "",
        "last_comment_note_id": "",
        "last_line": lines[-1] if lines else "",
        "has_error": False,
        "has_warning": False,
    }
    kw_re = re.compile(r"Current search keyword:\s*(.+)$")
    detail_re = re.compile(r"\[detail\] fetching note detail note_id=([^,\\s]+)")
    comment_re = re.compile(r"\[comments\] fetching comments for note_id=([^,\\s]+)")
    for line in lines:
        if "ERROR" in line:
            info["has_error"] = True
        if "WARNING" in line:
            info["has_warning"] = True
        kw_match = kw_re.search(line)
        if kw_match:
            info["current_keyword"] = kw_match.group(1).strip()
        detail_match = detail_re.search(line)
        if detail_match:
            info["last_detail_note_id"] = detail_match.group(1)
        comment_match = comment_re.search(line)
        if comment_match:
            info["last_comment_note_id"] = comment_match.group(1)
    return info


def _crawl_keyword_stats(
    conn: sqlite3.Connection,
    keywords: list[str],
    task_id: str = "",
    started_at: float = 0,
) -> list[dict]:
    if not keywords:
        return []
    ph = _kw_placeholders(keywords)
    rows = {
        kw: {
            "keyword": kw,
            "post_count": 0,
            "persisted_post_count": 0,
            "new_post_count": 0,
            "db_comment_count": 0,
            "platform_comment_count": 0,
            "last_note_ts": 0,
            "last_comment_ts": 0,
            "last_activity_ts": 0,
            "status": "等待",
        }
        for kw in keywords
    }

    cur = conn.cursor()
    if task_id:
        started_ms = int(started_at * 1000) if started_at else 0
        cur.execute(
            f"""
            SELECT h.keyword, COUNT(DISTINCT h.note_id),
                   COUNT(DISTINCT n.note_id),
                   COUNT(DISTINCT CASE WHEN n.add_ts >= ? THEN n.note_id END),
                   COALESCE(SUM(n.comment_count), 0), COALESCE(MAX(h.last_seen_ts), 0)
            FROM xhs_note_keyword_hit h
            LEFT JOIN xhs_note n ON n.note_id = h.note_id
            WHERE h.task_id = ? AND h.keyword IN ({ph})
            GROUP BY h.keyword
            """,
            [started_ms, task_id] + keywords,
        )
    else:
        cur.execute(
            f"""
            SELECT source_keyword, COUNT(*), COALESCE(SUM(comment_count), 0), COALESCE(MAX(add_ts), 0)
            FROM xhs_note
            WHERE source_keyword IN ({ph})
            GROUP BY source_keyword
            """,
            keywords,
        )
    for row in cur.fetchall():
        if task_id:
            kw, post_count, persisted_post_count, new_post_count, platform_comments, last_note_ts = row
        else:
            kw, post_count, platform_comments, last_note_ts = row
            persisted_post_count, new_post_count = post_count, 0
        item = rows.get(kw)
        if not item:
            continue
        item["post_count"] = post_count or 0
        item["persisted_post_count"] = persisted_post_count or 0
        item["new_post_count"] = new_post_count or 0
        item["platform_comment_count"] = platform_comments or 0
        item["last_note_ts"] = last_note_ts or 0
        item["last_activity_ts"] = max(item["last_activity_ts"], item["last_note_ts"])
        item["status"] = "已入库" if item["post_count"] else "等待"

    if task_id:
        started_ms = int(started_at * 1000) if started_at else 0
        cur.execute(
            f"""
            SELECT h.keyword, COUNT(DISTINCT c.id), COALESCE(MAX(c.add_ts), 0)
            FROM xhs_note_keyword_hit h
            JOIN xhs_note_comment c ON c.note_id = h.note_id
            WHERE h.task_id = ? AND h.keyword IN ({ph}) AND c.add_ts >= ?
            GROUP BY h.keyword
            """,
            [task_id] + keywords + [started_ms],
        )
    else:
        cur.execute(
            f"""
            SELECT n.source_keyword, COUNT(*), COALESCE(MAX(c.add_ts), 0)
            FROM xhs_note_comment c
            JOIN xhs_note n ON c.note_id = n.note_id
            WHERE n.source_keyword IN ({ph})
            GROUP BY n.source_keyword
            """,
            keywords,
        )
    for kw, comment_count, last_comment_ts in cur.fetchall():
        item = rows.get(kw)
        if not item:
            continue
        item["db_comment_count"] = comment_count or 0
        item["last_comment_ts"] = last_comment_ts or 0
        item["last_activity_ts"] = max(item["last_activity_ts"], item["last_comment_ts"])

    return [rows[kw] for kw in keywords]


def _crawl_task_speed(conn: sqlite3.Connection, keywords: list[str], task_id: str = "") -> dict:
    if not keywords:
        return {"15": {"posts": 0, "comments": 0}, "30": {"posts": 0, "comments": 0}, "60": {"posts": 0, "comments": 0}}
    ph = _kw_placeholders(keywords)
    now_ms = int(time.time() * 1000)
    speed = {}
    cur = conn.cursor()
    for minutes in (15, 30, 60):
        cutoff = now_ms - minutes * 60 * 1000
        if task_id:
            cur.execute(
                f"SELECT COUNT(DISTINCT note_id) FROM xhs_note_keyword_hit WHERE task_id = ? AND keyword IN ({ph}) AND first_seen_ts >= ?",
                [task_id] + keywords + [cutoff],
            )
        else:
            cur.execute(
                f"SELECT COUNT(*) FROM xhs_note WHERE source_keyword IN ({ph}) AND add_ts >= ?",
                keywords + [cutoff],
            )
        posts = cur.fetchone()[0] or 0
        if task_id:
            cur.execute(
                f"""
                SELECT COUNT(DISTINCT c.id)
                FROM xhs_note_comment c
                JOIN xhs_note_keyword_hit h ON h.note_id = c.note_id
                WHERE h.task_id = ? AND h.keyword IN ({ph}) AND c.add_ts >= ?
                """,
                [task_id] + keywords + [cutoff],
            )
        else:
            cur.execute(
                f"""
                SELECT COUNT(*)
                FROM xhs_note_comment c
                JOIN xhs_note n ON c.note_id = n.note_id
                WHERE n.source_keyword IN ({ph}) AND c.add_ts >= ?
                """,
                keywords + [cutoff],
            )
        comments = cur.fetchone()[0] or 0
        speed[str(minutes)] = {"posts": posts, "comments": comments}
    return speed


def _crawl_task_recent_notes(conn: sqlite3.Connection, task_id: str, limit: int = 12) -> list[dict]:
    if not task_id:
        return []
    cur = conn.cursor()
    cur.execute(
        """
        SELECT n.note_id, COALESCE(n.title, ''), COALESCE(n.desc, ''),
               COALESCE(n.liked_count, 0), COALESCE(n.comment_count, 0),
               COALESCE(MAX(h.last_seen_ts), 0), GROUP_CONCAT(DISTINCT h.keyword)
        FROM xhs_note_keyword_hit h
        JOIN xhs_note n ON n.note_id = h.note_id
        WHERE h.task_id = ?
        GROUP BY n.note_id, n.title, n.desc, n.liked_count, n.comment_count
        ORDER BY MAX(h.last_seen_ts) DESC
        LIMIT ?
        """,
        (task_id, limit),
    )
    return [
        {
            "note_id": row[0],
            "title": row[1] or "(无标题)",
            "desc": (row[2] or "")[:180],
            "liked_count": row[3] or 0,
            "platform_comment_count": row[4] or 0,
            "hit_ts": row[5] or 0,
            "keywords": [kw for kw in (row[6] or "").split(",") if kw],
        }
        for row in cur.fetchall()
    ]


def _crawl_task_totals(conn: sqlite3.Connection, task_id: str, started_at: float = 0) -> dict:
    cur = conn.cursor()
    started_ms = int(started_at * 1000) if started_at else 0
    cur.execute(
        """
        SELECT COUNT(*), COUNT(persisted_note_id),
               COALESCE(SUM(CASE WHEN add_ts >= ? THEN 1 ELSE 0 END), 0),
               COALESCE(SUM(comment_count), 0), COALESCE(MAX(last_seen_ts), 0)
        FROM (
            SELECT h.note_id, n.note_id AS persisted_note_id, n.add_ts,
                   COALESCE(n.comment_count, 0) AS comment_count,
                   MAX(h.last_seen_ts) AS last_seen_ts
            FROM xhs_note_keyword_hit h
            LEFT JOIN xhs_note n ON n.note_id = h.note_id
            WHERE h.task_id = ?
            GROUP BY h.note_id, n.note_id, n.add_ts, n.comment_count
        )
        """,
        (started_ms, task_id),
    )
    hits, persisted_posts, new_posts, platform_comments, last_hit_ts = cur.fetchone()
    cur.execute(
        """
        SELECT COUNT(DISTINCT c.id), COALESCE(MAX(c.add_ts), 0)
        FROM xhs_note_comment c
        JOIN xhs_note_keyword_hit h ON h.note_id = c.note_id
        WHERE h.task_id = ? AND c.add_ts >= ?
        """,
        (task_id, started_ms),
    )
    db_comments, last_comment_ts = cur.fetchone()
    return {
        "posts": hits or 0,
        "persisted_posts": persisted_posts or 0,
        "new_posts": new_posts or 0,
        "existing_posts": max(0, (persisted_posts or 0) - (new_posts or 0)),
        "db_comments": db_comments or 0,
        "platform_comments": platform_comments or 0,
        "last_activity_ts": max(last_hit_ts or 0, last_comment_ts or 0),
    }


def _crawl_task_summary(task: dict | None) -> dict | None:
    if not task:
        return None
    keywords = task.get("keywords") or []
    config = task.get("config") or {}
    log_lines, log_total = _read_tail_lines(task.get("log_path"), 80)
    log_info = _parse_crawl_log(log_lines)
    started_at = task.get("started_at") or task.get("created_at") or 0
    runtime_seconds = max(0, int(time.time() - started_at)) if started_at else 0
    summary = {
        "id": task.get("id"),
        "account_id": task.get("account_id") or config.get("account_id") or DEFAULT_ACCOUNT_ID,
        "name": task.get("name"),
        "status": task.get("status"),
        "created_at": task.get("created_at"),
        "started_at": task.get("started_at"),
        "completed_at": task.get("completed_at"),
        "runtime_seconds": runtime_seconds,
        "keywords": keywords,
        "config": config,
        "log_path": task.get("log_path"),
        "worker_pid": task.get("worker_pid"),
        "worker_alive": _pid_alive(task.get("worker_pid")),
        "error_message": task.get("error_message"),
        "exit_code": task.get("exit_code"),
        "log": {
            **log_info,
            "lines": log_lines[-30:],
            "total": log_total,
        },
        "keyword_stats": [],
        "recent_notes": [],
        "speed": {"15": {"posts": 0, "comments": 0}, "30": {"posts": 0, "comments": 0}, "60": {"posts": 0, "comments": 0}},
        "totals": {
            "posts": 0,
            "db_comments": 0,
            "platform_comments": 0,
            "last_activity_ts": 0,
        },
    }

    conn = _with_crawler_db(summary["account_id"])
    if not conn:
        return summary
    try:
        keyword_stats = _crawl_keyword_stats(conn, keywords, task.get("id") or "", started_at)
        summary["keyword_stats"] = keyword_stats
        summary["speed"] = _crawl_task_speed(conn, keywords, task.get("id") or "")
        summary["totals"] = _crawl_task_totals(conn, task.get("id") or "", started_at)
        summary["recent_notes"] = _crawl_task_recent_notes(conn, task.get("id") or "")
        current_keyword = summary["log"].get("current_keyword")
        for item in summary["keyword_stats"]:
            if item["keyword"] == current_keyword and task.get("status") in ("starting", "running", "stopping"):
                item["status"] = "当前"
    finally:
        conn.close()
    return summary


@app.route("/api/crawl-tasks/active-summary")
def api_active_crawl_task_summary():
    from crawl_task_manager import list_crawl_tasks

    tasks = list_crawl_tasks(archived=False)
    active_tasks = [
        task for task in tasks
        if task.get("status") in ("starting", "running", "stopping")
    ]
    active = active_tasks[0] if active_tasks else None
    recent = tasks[0] if tasks else None
    risk_policies = {}
    for account in list_accounts(include_disabled=True):
        status = get_risk_policy_status(account["user_data_dir"])
        status["account_id"] = account["account_id"]
        risk_policies[account["account_id"]] = status
    return jsonify({
        "active_task": _crawl_task_summary(active),
        "active_tasks": [_crawl_task_summary(task) for task in active_tasks],
        "recent_task": _crawl_task_summary(recent) if not active and recent else None,
        "risk_policy": risk_policies.get(DEFAULT_ACCOUNT_ID, get_risk_policy_status()),
        "risk_policies": risk_policies,
        "ts": time.time(),
    })


@app.route("/api/crawl-tasks/<task_id>")
def api_get_crawl_task(task_id):
    from crawl_task_manager import get_crawl_task
    task = get_crawl_task(task_id)
    if not task:
        return jsonify({"error": "not found"}), 404
    return jsonify(task)


@app.route("/api/crawl-tasks", methods=["POST"])
def api_create_crawl_task():
    from crawl_task_manager import create_crawl_task
    data = request.get_json(force=True)
    name = str(data.get("name", "") or "").strip()
    keywords = _normalize_crawl_keywords(data.get("keywords", []))
    if not name or not keywords:
        return jsonify({"error": "name and keywords required"}), 400
    if len(keywords) > 80:
        return jsonify({"error": "a task may contain at most 80 keywords"}), 400
    try:
        raw_config = data.get("config", {}) or {}
        selected_account_id = str(
            data.get("account_id")
            or raw_config.get("account_id")
            or DEFAULT_ACCOUNT_ID
        ).strip()
        config = _normalize_crawl_config(raw_config)
        config, account = bind_task_config(config, account_id=selected_account_id)
    except (AccountRegistryError, TypeError, ValueError) as exc:
        return jsonify({"error": f"invalid crawl task config: {exc}"}), 400
    task_id = create_crawl_task(
        name,
        keywords,
        config,
        account_id=account["account_id"],
    )
    return jsonify({"ok": True, "id": task_id})


def _launch_crawl_task(task_id):
    from crawl_task_manager import claim_crawl_task, fail_crawl_task_start, get_crawl_task, set_crawl_worker_pid

    task = get_crawl_task(task_id)
    if not task:
        return jsonify({"error": "task not found"}), 404
    task_config = task.get("config") or {}
    try:
        task_config, account = _runtime_account_config(task, task_config)
    except AccountRegistryError as exc:
        return jsonify({"error": str(exc)}), 409
    if _account_has_active_task(account["account_id"], exclude_task_id=task_id):
        return jsonify({"error": "selected account already has an active task"}), 409
    profile_busy = _profile_busy_response(account)
    if profile_busy:
        return profile_busy
    dry_run = bool(task_config.get("dry_run"))
    if not dry_run:
        decision = reserve_launch(
            task_id=task_id,
            task_kind="search",
            config=task_config,
            total_posts=int(task_config.get("max_count", 0) or 0),
        )
        if not decision.allowed:
            return jsonify({"error": decision.reason, "risk_policy": decision.as_dict()}), 429

    claimed, error = claim_crawl_task(task_id)
    if not claimed:
        if not dry_run:
            abort_launch(task_id, task_config.get("user_data_dir", ""), error)
        status = 404 if error == "task not found" else 409
        return jsonify({"error": error}), status
    enable_cdp = bool(task_config.get("enable_cdp"))
    require_cdp = bool(task_config.get("require_cdp"))
    worker_env = os.environ.copy()
    worker_env["MEDIACRAWLER_ACCOUNT"] = account["account_id"]
    worker_env["MEDIACRAWLER_SQLITE_DB_PATH"] = account["sqlite_db_path"]
    worker_env["MEDIACRAWLER_USER_DATA_DIR"] = account["user_data_dir"]
    worker_env["MEDIACRAWLER_BROWSER_PATH"] = account["browser_path"]
    worker_env["MEDIACRAWLER_TASK_ID"] = task_id
    worker_env["MEDIACRAWLER_LOG_PATH"] = _account_worker_log_path(
        account["account_id"], task_id
    )
    if not dry_run and decision.canary:
        worker_env["MEDIACRAWLER_RISK_CANARY"] = "true"
    if enable_cdp:
        try:
            cdp_status = start_cdp_chrome(task_config.get("cdp_debug_port"))
        except Exception as exc:
            fail_crawl_task_start(task_id, f"crawler browser unavailable: {exc}")
            if not dry_run:
                abort_launch(task_id, task_config.get("user_data_dir", ""), str(exc))
            return jsonify({"error": f"crawler browser unavailable: {exc}"}), 500
        if not cdp_status.get("ok") and require_cdp:
            fail_crawl_task_start(task_id, cdp_status.get("error") or "CDP Chrome unavailable")
            if not dry_run:
                abort_launch(task_id, task_config.get("user_data_dir", ""), cdp_status.get("error") or "CDP unavailable")
            return jsonify({"error": cdp_status}), 500
        if cdp_status.get("ok") and cdp_status.get("port"):
            # The preferred port may be occupied.  Pass the actual reusable
            # browser port to the worker instead of silently falling back to a
            # newly launched standard browser.
            worker_env["MEDIACRAWLER_TASK_CDP_DEBUG_PORT"] = str(cdp_status["port"])
    uv = shutil.which("uv")
    command = ([uv, "run", "python"] if uv else [sys.executable]) + [
        os.path.join(DASHBOARD_DIR, "crawl_runner.py"),
        "--task-id",
        task_id,
    ]
    try:
        worker = subprocess.Popen(
            command,
            cwd=MEDIACRAWLER_ROOT,
            env=worker_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        set_crawl_worker_pid(task_id, worker.pid)
    except Exception as exc:
        fail_crawl_task_start(task_id, str(exc))
        if not dry_run:
            abort_launch(task_id, task_config.get("user_data_dir", ""), str(exc))
        return jsonify({"error": f"failed to start crawl worker: {exc}"}), 500
    return jsonify({"ok": True, "pid": worker.pid, "status": "starting"}), 202


@app.route("/api/crawl-tasks/<task_id>/start", methods=["POST"])
def api_start_crawl_task(task_id):
    return _launch_crawl_task(task_id)


@app.route("/api/risk-policy/status")
def api_risk_policy_status():
    account_id = str(request.args.get("account_id", "") or "").strip()
    if account_id:
        try:
            account = get_account(account_id)
        except AccountRegistryError as exc:
            return jsonify({"error": str(exc)}), 400
        if not account:
            return jsonify({"error": "account not found"}), 404
        status = get_risk_policy_status(account["user_data_dir"])
        status["account_id"] = account["account_id"]
        return jsonify(status)
    user_data_dir = request.args.get("user_data_dir", DEFAULT_USER_DATA_DIR)
    return jsonify(get_risk_policy_status(user_data_dir))


def _auto_task_config(task: dict) -> dict:
    config = task.get("config")
    if isinstance(config, dict):
        return config
    try:
        return json.loads(task.get("config_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _auto_task_account_id(task: dict) -> str:
    config = _auto_task_config(task)
    return str(
        task.get("account_id") or config.get("account_id") or DEFAULT_ACCOUNT_ID
    ).strip()


def _auto_start_once() -> bool:
    """Fill idle per-account slots; one account's denial never blocks another."""
    from crawl_task_manager import list_crawl_tasks
    from task_manager import list_tasks

    crawl_tasks = list_crawl_tasks(archived=False)
    comment_tasks = list_tasks(archived=False)
    active_statuses = {"starting", "running", "stopping"}
    active_accounts = {
        _auto_task_account_id(task)
        for task in crawl_tasks + comment_tasks
        if task.get("status") in active_statuses
    }
    available_slots = max(0, MAX_PARALLEL_XHS_ACCOUNTS - len(active_accounts))
    if not available_slots:
        return False

    attempted_accounts: set[str] = set()
    started = 0
    # Searches go first so each account can accumulate clean canaries before
    # that same account's queued comment work is considered.
    candidates = [
        (task, "search") for task in reversed(crawl_tasks)
    ] + [
        (task, "comment") for task in reversed(comment_tasks)
    ]
    for task, task_kind in candidates:
        if started >= available_slots:
            break
        if task.get("status") != "pending":
            continue
        if _auto_task_config(task).get("start_mode") != "auto":
            continue
        account_id = _auto_task_account_id(task)
        if account_id in active_accounts or account_id in attempted_accounts:
            continue
        attempted_accounts.add(account_id)
        result = (
            _launch_crawl_task(task["id"])
            if task_kind == "search"
            else _launch_comment_task(task["id"])
        )
        status_code = result[1] if isinstance(result, tuple) else result.status_code
        if status_code != 202:
            logger.info(
                "[risk-scheduler] account=%s kept queued %s task %s status=%s",
                account_id,
                task_kind,
                task["id"],
                status_code,
            )
            continue
        active_accounts.add(account_id)
        started += 1
        logger.info(
            "[risk-scheduler] account=%s started queued %s task %s",
            account_id,
            task_kind,
            task["id"],
        )
    return started > 0


def risk_scheduler_loop() -> None:
    while True:
        try:
            with app.app_context():
                _auto_start_once()
        except Exception:
            logger.exception("[risk-scheduler] scheduler iteration failed")
        time.sleep(max(5, RISK_SCHEDULER_INTERVAL))


@app.route("/api/crawl-tasks/<task_id>/cancel", methods=["POST"])
def api_cancel_crawl_task(task_id):
    from crawl_task_manager import (
        clear_crawl_task_stop_request,
        get_crawl_task,
        mark_crawl_task_cancelled,
        request_crawl_task_stop,
    )

    task = get_crawl_task(task_id)
    if not task:
        return jsonify({"error": "task not found"}), 404
    if task.get("status") not in ("starting", "running"):
        return jsonify({"error": f"task is {task.get('status')}, cannot cancel"}), 409
    ok, error = request_crawl_task_stop(task_id)
    if not ok:
        return jsonify({"error": error}), 409
    pid = task.get("worker_pid")
    killed = False
    kill_errors = []
    if pid:
        try:
            os.killpg(int(pid), 15)
            killed = True
        except ProcessLookupError:
            killed = True
            mark_crawl_task_cancelled(task_id)
        except Exception as exc:
            kill_errors.append(str(exc))
    if not pid:
        mark_crawl_task_cancelled(task_id)
    elif kill_errors:
        clear_crawl_task_stop_request(task_id, "停止失败：" + "; ".join(kill_errors))
        return jsonify({"ok": False, "killed": False, "errors": kill_errors}), 500
    _record_task_cancellation(task, "search")
    return jsonify({"ok": True, "killed": killed, "errors": kill_errors})


@app.route("/api/crawl-tasks/<task_id>/archive", methods=["POST"])
def api_archive_crawl_task(task_id):
    from crawl_task_manager import set_crawl_task_archived
    ok, error = set_crawl_task_archived(task_id, archived=True)
    if not ok:
        status = 404 if error == "task not found" else 409
        return jsonify({"error": error}), status
    return jsonify({"ok": True, "archived": True})


@app.route("/api/crawl-tasks/<task_id>/unarchive", methods=["POST"])
def api_unarchive_crawl_task(task_id):
    from crawl_task_manager import set_crawl_task_archived
    ok, error = set_crawl_task_archived(task_id, archived=False)
    if not ok:
        status = 404 if error == "task not found" else 409
        return jsonify({"error": error}), status
    return jsonify({"ok": True, "archived": False})


@app.route("/api/crawl-tasks/<task_id>/log")
def api_get_crawl_task_log(task_id):
    from crawl_task_manager import get_crawl_task

    task = get_crawl_task(task_id)
    if not task:
        return jsonify({"error": "task not found", "lines": []}), 404
    log_path = task.get("log_path")
    if not log_path or not os.path.exists(log_path):
        return jsonify({"lines": [], "total": 0, "log_path": log_path})
    lines = request.args.get("lines", 160, type=int)
    lines = max(1, min(lines, 800))
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.read().splitlines()
    except Exception as exc:
        return jsonify({"error": str(exc), "lines": [], "log_path": log_path}), 500
    return jsonify({"lines": all_lines[-lines:], "total": len(all_lines), "log_path": log_path})


@app.route("/api/worth-digging")
def api_worth_digging():
    """Get high-value posts worth digging for comments."""
    keywords, _ = get_config_values()
    conn = _with_crawler_db()
    if not conn:
        return jsonify({"error": "no DB connection"})
    try:
        rows, note_keywords = _asset_scope_rows(conn, keywords)
        posts = []
        for r in rows:
            post = {
                "note_id": r[0],
                "title": r[1],
                "source_keyword": r[3],
                "keywords": note_keywords.get(r[0], []),
                "liked_count": r[4] or 0,
                "comment_count": r[6] or 0,
                "desc_length": len(r[2] or ""),
                "db_comment_count": r[9] or 0,
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


@app.route("/api/source-keys")
def api_source_keys():
    """List all source_keyword values currently stored in crawler DB."""
    conn = None
    try:
        conn = _with_crawler_db()
        if not conn:
            return jsonify({"source_keys": []})
        cur = conn.cursor()
        cur.execute("""
            WITH db_comments AS (
                SELECT n.source_keyword AS source_key, COUNT(c.id) AS db_comment_count
                FROM xhs_note n
                JOIN xhs_note_comment c ON c.note_id = n.note_id
                WHERE COALESCE(TRIM(n.source_keyword), '') != ''
                GROUP BY n.source_keyword
            )
            SELECT
                n.source_keyword AS source_key,
                COUNT(*) AS post_count,
                COALESCE(SUM(n.comment_count), 0) AS platform_comment_count,
                COALESCE(MAX(n.add_ts), 0) AS latest_add_ts,
                COALESCE(dc.db_comment_count, 0) AS db_comment_count
            FROM xhs_note n
            LEFT JOIN db_comments dc ON dc.source_key = n.source_keyword
            WHERE COALESCE(TRIM(n.source_keyword), '') != ''
            GROUP BY n.source_keyword
            ORDER BY latest_add_ts DESC, post_count DESC, source_key ASC
        """)
        rows = []
        for r in cur.fetchall():
            rows.append({
                "source_key": r[0],
                "post_count": int(r[1] or 0),
                "platform_comment_count": int(r[2] or 0),
                "latest_add_ts": int(r[3] or 0),
                "db_comment_count": int(r[4] or 0),
            })
        return jsonify({"source_keys": rows})
    finally:
        if conn:
            conn.close()


@app.route("/api/groups/save", methods=["POST"])
def api_save_group():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    keywords = data.get("keywords", [])
    hidden_keywords = data.get("hidden_keywords")
    max_notes = data.get("max_notes", 200)
    if not name:
        return jsonify({"error": "name required"}), 400
    if not isinstance(keywords, list):
        return jsonify({"error": "keywords must be a list"}), 400
    if hidden_keywords is not None and not isinstance(hidden_keywords, list):
        return jsonify({"error": "hidden_keywords must be a list"}), 400
    save_group(name, keywords, max_notes, hidden_keywords)
    activate_group(name)
    return jsonify({"ok": True})


@app.route("/api/groups/rename", methods=["POST"])
def api_rename_group():
    data = request.get_json(force=True)
    ok, error = rename_group(data.get("old_name", ""), data.get("new_name", ""))
    if not ok:
        return jsonify({"error": error}), 400
    return jsonify({"ok": True})


@app.route("/api/groups/copy", methods=["POST"])
def api_copy_group():
    data = request.get_json(force=True)
    ok, error = copy_group(data.get("source_name", ""), data.get("new_name", ""))
    if not ok:
        return jsonify({"error": error}), 400
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
            elif "RiskControlError" in line or "[risk-control-stop]" in line:
                patterns["RiskControlError"] = patterns.get("RiskControlError", 0) + 1
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
    logger.info(f'[dashboard] MediaCrawler root: {MEDIACRAWLER_ROOT}')
    logger.info(f"[dashboard] Crawler DB: {_crawler_db_path or 'NOT FOUND'}")
    ensure_crawler_db_indexes()
    init_dashboard_db()
    take_snapshot()
    bg = threading.Thread(target=snapshot_loop, daemon=True)
    bg.start()
    logger.info(f'[dashboard] Snapshot worker started (interval={SNAPSHOT_INTERVAL}s)')
    if RISK_SCHEDULER_ENABLED:
        risk_bg = threading.Thread(target=risk_scheduler_loop, daemon=True)
        risk_bg.start()
        logger.info(f'[dashboard] Risk scheduler started (interval={RISK_SCHEDULER_INTERVAL}s)')
    logger.info(f'[dashboard] Starting on http://{HOST}:{PORT}')
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
