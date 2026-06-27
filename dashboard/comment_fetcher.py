#!/usr/bin/env python3
"""Execute dashboard comment-supplement tasks through MediaCrawler detail mode."""

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, List
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

MEDIACRAWLER_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = MEDIACRAWLER_ROOT / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))

from task_manager import (  # noqa: E402
    claim_task,
    finish_task,
    get_task,
    get_task_posts,
    start_task,
    update_post_status,
)


def get_cdp_debug_port() -> int:
    try:
        return int(os.getenv("MEDIACRAWLER_CDP_DEBUG_PORT", "9222"))
    except ValueError:
        return 9222


def cdp_websocket_from_active_port(port: int) -> str:
    candidates = [
        Path.home() / "Library/Application Support/Google/Chrome/DevToolsActivePort",
        Path.home() / "Library/Application Support/Google/Chrome/Default/DevToolsActivePort",
        MEDIACRAWLER_ROOT / "browser_data" / "chrome-cdp-debug" / "DevToolsActivePort",
        MEDIACRAWLER_ROOT / "browser_data" / f"chrome-cdp-debug-{port}" / "DevToolsActivePort",
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
            continue
    return ""


def check_cdp_remote_debugging(port: int = None, timeout: float = 2.0) -> tuple[bool, str]:
    """Check whether Chrome remote debugging is enabled and reachable."""
    port = port or get_cdp_debug_port()
    url = f"http://127.0.0.1:{port}/json/version"
    browser = ""
    web_socket = ""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return False, f"CDP endpoint returned HTTP {response.status}: {url}"
            data = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
            browser = data.get("Browser", "")
            web_socket = data.get("webSocketDebuggerUrl", "")
    except urllib.error.URLError as exc:
        web_socket = cdp_websocket_from_active_port(port)
        if not web_socket:
            return False, (
                f"CDP remote debugging is not reachable on port {port}. "
                "Open Chrome, enable chrome://inspect/#remote-debugging, "
                f"or start Chrome with --remote-debugging-port={port}. Detail: {exc}"
            )
    except Exception as exc:
        return False, f"CDP check failed on port {port}: {exc}"
    if not browser and not web_socket:
        return False, f"CDP endpoint on port {port} responded but did not look like Chrome DevTools."

    async def verify_playwright():
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            await playwright.chromium.connect_over_cdp(
                web_socket or f"http://127.0.0.1:{port}",
                timeout=max(timeout, 10.0) * 1000,
            )

    try:
        asyncio.run(verify_playwright())
    except Exception as exc:
        return False, (
            f"CDP endpoint is reachable on port {port}, but Playwright cannot connect: {exc}. "
            "Use the Dashboard CDP Chrome launcher to start a compatible remote-debugging browser."
        )
    return True, f"CDP remote debugging ready on port {port}: {browser or web_socket}"


def chunks(items: List[Dict], size: int) -> Iterable[List[Dict]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


class CommentTaskExecutor:
    def __init__(self, db_path: str, max_comments: int, dry_run: bool = False,
                 user_data_dir: str = "%s_user_data_dir_account02",
                 get_sub_comments: bool = False, max_sub_comments: int = 200):
        self.db_path = os.path.abspath(db_path)
        self.max_comments = max_comments
        self.dry_run = dry_run
        self.user_data_dir = user_data_dir
        self.get_sub_comments = get_sub_comments
        self.max_sub_comments = max_sub_comments
        self.conn = sqlite3.connect(self.db_path, timeout=30)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        self.conn.close()

    def saved_comment_count(self, note_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM xhs_note_comment WHERE note_id = ?", (note_id,)
        ).fetchone()[0]

    def get_post(self, note_id: str) -> Dict:
        row = self.conn.execute(
            """SELECT note_id, note_url, xsec_token, title
               FROM xhs_note WHERE note_id = ?""",
            (note_id,),
        ).fetchone()
        return dict(row) if row else {}

    @staticmethod
    def detail_url(post: Dict) -> str:
        """Build a detail URL carrying the token required by the XHS API."""
        note_id = post.get("note_id") or ""
        token = post.get("xsec_token") or ""
        raw_url = post.get("note_url") or f"https://www.xiaohongshu.com/explore/{note_id}"
        parsed = urlparse(raw_url)
        query = parse_qs(parsed.query)
        if token:
            query["xsec_token"] = [token]
        query.setdefault("xsec_source", ["pc_search"])
        effective_token = query.get("xsec_token", [""])[0]
        if not note_id or not effective_token:
            raise ValueError(f"missing note_id or xsec_token for {note_id or '<unknown>'}")
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    def crawler_command(self, urls: List[str]) -> List[str]:
        uv = shutil.which("uv")
        if uv:
            prefix = [uv, "run", "main.py"]
        else:
            prefix = [sys.executable, "main.py"]
        return prefix + [
            "--platform", "xhs",
            "--type", "detail",
            "--specified_id", ",".join(urls),
            "--get_comment", "true",
            "--get_sub_comment", "true" if self.get_sub_comments else "false",
            "--save_data_option", "sqlite",
            "--max_comments_count_singlenotes", str(self.max_comments),
            "--max_sub_comments_count_singlenotes", str(self.max_sub_comments),
            "--max_concurrency_num", "1",
        ]

    def run_batch(self, task_id: str, posts: List[Dict], log_file) -> bool:
        prepared = []
        for task_post in posts:
            note_id = task_post["note_id"]
            before = self.saved_comment_count(note_id)
            post = self.get_post(note_id)
            try:
                url = self.detail_url(post)
            except ValueError as exc:
                update_post_status(
                    task_id, note_id, "failed", before, str(exc), before
                )
                print(f"[failed] {exc}", file=log_file, flush=True)
                continue
            update_post_status(task_id, note_id, "running", before, None, before)
            prepared.append((task_post, before, url))

        if not prepared:
            return False

        command = self.crawler_command([item[2] for item in prepared])
        printable = " ".join(json.dumps(part, ensure_ascii=False) for part in command)
        print(f"[command] {printable}", file=log_file, flush=True)
        if self.dry_run:
            print("[dry-run] crawler was not started", file=log_file, flush=True)
            for task_post, before, _ in prepared:
                update_post_status(task_id, task_post["note_id"], "pending", before, None, before)
            return True

        try:
            process = subprocess.Popen(
                command,
                cwd=MEDIACRAWLER_ROOT,
                env=self.crawler_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="", file=log_file, flush=True)
            returncode = process.wait()
        except KeyboardInterrupt:
            for task_post, before, _ in prepared:
                note_id = task_post["note_id"]
                after = self.saved_comment_count(note_id)
                update_post_status(
                    task_id, note_id, "failed", after, "crawler interrupted", before
                )
            raise
        except Exception as exc:
            returncode = 1
            print(f"[crawler-launch-error] {exc}", file=log_file, flush=True)

        success = returncode == 0
        for task_post, before, _ in prepared:
            note_id = task_post["note_id"]
            after = self.saved_comment_count(note_id)
            error = None if success else f"crawler exited with code {returncode}"
            update_post_status(
                task_id, note_id, "completed" if success else "failed",
                after, error, before,
            )
            print(
                f"[{('completed' if success else 'failed')}] {note_id}: "
                f"before={before} after={after} added={max(0, after - before)}",
                file=log_file,
                flush=True,
            )
        return success

    def crawler_environment(self) -> Dict[str, str]:
        """Use standard browser mode for background tasks.

        CDP can still be enabled explicitly with MEDIACRAWLER_ENABLE_CDP=true,
        but dashboard tasks should not block on CDP availability.
        """
        env = os.environ.copy()
        env.setdefault("MEDIACRAWLER_ENABLE_CDP", "false")
        env.setdefault("MEDIACRAWLER_REQUIRE_CDP", "false")
        env.setdefault("MEDIACRAWLER_CDP_DEBUG_PORT", str(get_cdp_debug_port()))
        default_chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.exists(default_chrome):
            env.setdefault("MEDIACRAWLER_BROWSER_PATH", default_chrome)
        env.setdefault("MEDIACRAWLER_USER_DATA_DIR", self.user_data_dir)
        return env


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a comment supplement task")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--db-path", default="database/sqlite_tables.db")
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--max-comments", type=int)
    parser.add_argument("--max-sub-comments", type=int)
    parser.add_argument("--delay", type=float)
    parser.add_argument("--limit", type=int, default=0, help="Only process N pending posts")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument(
        "--user-data-dir", default="%s_user_data_dir_account02",
        help="Browser profile template under browser_data",
    )
    parser.add_argument(
        "--get-sub-comments", action="store_true", default=None,
        help="Also expand second-level comments; disabled by default to bound task size",
    )
    args = parser.parse_args()

    task = get_task(args.task_id)
    if not task:
        parser.error(f"task not found: {args.task_id}")
    try:
        task_config = json.loads(task.get("config_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        task_config = {}
    args.batch_size = args.batch_size or int(task_config.get("batch_size", 5))
    args.max_comments = args.max_comments or int(task_config.get("max_comments", 200))
    if args.max_sub_comments is None:
        args.max_sub_comments = int(task_config.get("max_sub_comments", 200))
    if args.delay is None:
        args.delay = float(task_config.get("delay", 5))
    if args.get_sub_comments is None:
        args.get_sub_comments = bool(task_config.get("get_sub_comments", False))
    if args.batch_size < 1 or args.max_comments < 1 or args.max_sub_comments < 0:
        parser.error("batch-size and max-comments must be positive")

    if os.getenv("MEDIACRAWLER_ENABLE_CDP", "false").lower() in ("1", "true", "yes"):
        cdp_ok, cdp_message = check_cdp_remote_debugging()
        if not cdp_ok and os.getenv("MEDIACRAWLER_REQUIRE_CDP", "false").lower() in ("1", "true", "yes"):
            parser.error(cdp_message)
        print(f"[cdp] {cdp_message}")

    posts = get_task_posts(args.task_id, status="failed" if args.retry_failed else "pending")
    if args.limit:
        posts = posts[:args.limit]
    if not posts:
        print("No pending posts to process")
        return 0

    db_path = args.db_path
    if not os.path.isabs(db_path):
        db_path = str(MEDIACRAWLER_ROOT / db_path)
    log_dir = DASHBOARD_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{args.task_id}-{int(time.time())}.log"

    executor = CommentTaskExecutor(
        db_path, args.max_comments, args.dry_run, args.user_data_dir,
        args.get_sub_comments, args.max_sub_comments,
    )
    if not args.dry_run:
        if task["status"] in ("pending", "completed_with_errors"):
            claimed, claim_error = claim_task(
                args.task_id, retry_failed=args.retry_failed
            )
            if not claimed:
                executor.close()
                parser.error(claim_error)
        elif task["status"] != "starting":
            executor.close()
            parser.error(f"task is {task['status']}, cannot start")
        start_task(args.task_id, str(log_path))
    all_ok = True
    interrupted = False
    fatal_error = None
    try:
        with log_path.open("a", encoding="utf-8") as log_file:
            for batch_number, batch in enumerate(chunks(posts, args.batch_size), 1):
                print(f"[batch {batch_number}] posts={len(batch)}", file=log_file, flush=True)
                all_ok = executor.run_batch(args.task_id, batch, log_file) and all_ok
                if not args.dry_run and args.delay and batch_number * args.batch_size < len(posts):
                    time.sleep(args.delay)
    except KeyboardInterrupt:
        interrupted = True
        all_ok = False
        print("Task interrupted", file=sys.stderr)
    except Exception as exc:
        all_ok = False
        fatal_error = str(exc)
        with log_path.open("a", encoding="utf-8") as log_file:
            traceback.print_exc(file=log_file)
        for post in get_task_posts(args.task_id, status="running"):
            after = executor.saved_comment_count(post["note_id"])
            update_post_status(
                args.task_id, post["note_id"], "failed", after,
                fatal_error, post["comment_count_before"],
            )
    finally:
        executor.close()

    finish_task(args.task_id)
    print(f"Task {args.task_id} {'finished' if all_ok else 'finished with errors'}; log={log_path}")
    return 130 if interrupted else (0 if all_ok else 1)


if __name__ == "__main__":
    raise SystemExit(main())
