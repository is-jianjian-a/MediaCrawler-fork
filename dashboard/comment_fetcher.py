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
from contextlib import nullcontext
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

MEDIACRAWLER_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = MEDIACRAWLER_ROOT / "dashboard"
sys.path.insert(0, str(MEDIACRAWLER_ROOT))
sys.path.insert(0, str(DASHBOARD_DIR))

from config.runtime_paths import BROWSER_DATA_ROOT, LEGACY_CONTENT_DB, LOG_ROOT
from tools.app_runner import RISK_CONTROL_EXIT_CODE
from tools.browser_safety import BrowserPathError, validate_automation_browser_path
try:
    from dashboard.risk_policy import (
        assert_launch_reserved,
        confirm_launch,
        launch_lease_heartbeat,
        record_completion,
    )
except ModuleNotFoundError:
    from risk_policy import (  # type: ignore[no-redef]
        assert_launch_reserved,
        confirm_launch,
        launch_lease_heartbeat,
        record_completion,
    )
try:
    from dashboard.account_registry import (
        DEFAULT_ACCOUNT_ID,
        AccountRegistryError,
        bind_task_config,
    )
except ModuleNotFoundError:
    from account_registry import (  # type: ignore[no-redef]
        DEFAULT_ACCOUNT_ID,
        AccountRegistryError,
        bind_task_config,
    )
try:
    from dashboard.profile_lock import acquire_profile_lock
except ModuleNotFoundError:
    from profile_lock import acquire_profile_lock  # type: ignore[no-redef]
try:
    from dashboard.xhs_data_model import finish_task_run, mark_task_run_running
except ModuleNotFoundError:
    from xhs_data_model import (  # type: ignore[no-redef]
        finish_task_run,
        mark_task_run_running,
    )

from task_manager import (  # noqa: E402
    finish_task,
    get_task,
    get_task_posts,
    start_task,
    update_post_status,
)
try:
    from dashboard.rate_policy import (
        COMMENTS_RATE,
        DEFAULT_COMMENT_TASK_BATCH_DELAY,
        DEFAULT_COMMENT_TASK_BATCH_SIZE,
        DEFAULT_FIRST_LEVEL_COMMENTS,
    )
except ModuleNotFoundError:  # Support direct script execution.
    from rate_policy import (  # type: ignore[no-redef]
        COMMENTS_RATE,
        DEFAULT_COMMENT_TASK_BATCH_DELAY,
        DEFAULT_COMMENT_TASK_BATCH_SIZE,
        DEFAULT_FIRST_LEVEL_COMMENTS,
    )
import logging
import re
logger = logging.getLogger("MediaCrawler")

DEFAULT_COMMENT_PUBLISH_DATE_AFTER = "2000-01-01"


def validate_standard_browser_path(browser_path: str) -> str:
    """Require an explicit executable that is not the user's system Chrome."""
    try:
        return validate_automation_browser_path(browser_path)
    except BrowserPathError as exc:
        raise ValueError(str(exc)) from exc


def validate_comment_publish_date_after(value: str) -> str:
    """Validate the explicit historical floor used by comment supplement tasks."""
    raw_value = str(value or DEFAULT_COMMENT_PUBLISH_DATE_AFTER).strip()
    try:
        parsed = datetime.strptime(raw_value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("publish_date_after must use YYYY-MM-DD format") from exc
    if parsed > date.today():
        raise ValueError("publish_date_after must not be in the future")
    return parsed.isoformat()


def get_cdp_debug_port() -> int:
    try:
        return int(os.getenv("MEDIACRAWLER_CDP_DEBUG_PORT", "9222"))
    except ValueError:
        return 9222


def cdp_websocket_from_active_port(port: int) -> str:
    candidates = [
        Path.home() / "Library/Application Support/Google/Chrome/DevToolsActivePort",
        Path.home() / "Library/Application Support/Google/Chrome/Default/DevToolsActivePort",
        BROWSER_DATA_ROOT / "chrome-cdp-debug" / "DevToolsActivePort",
        BROWSER_DATA_ROOT / f"chrome-cdp-debug-{port}" / "DevToolsActivePort",
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
            logger.exception(f"Unhandled exception in cdp_websocket_from_active_port()")
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
                 get_sub_comments: bool = False, max_sub_comments: int = 200,
                 min_sleep: int = COMMENTS_RATE.min_sleep,
                 max_sleep: int = COMMENTS_RATE.max_sleep,
                 comment_sleep: int = COMMENTS_RATE.comment_sleep,
                 max_concurrency: int = COMMENTS_RATE.max_concurrency,
                 browser_path: str = "", inter_note_sleep: float = 0,
                 publish_date_after: str = DEFAULT_COMMENT_PUBLISH_DATE_AFTER,
                 account_id: str = DEFAULT_ACCOUNT_ID):
        self.db_path = os.path.abspath(db_path)
        self.account_id = account_id
        self.max_comments = max_comments
        self.dry_run = dry_run
        self.user_data_dir = user_data_dir
        self.get_sub_comments = get_sub_comments
        self.max_sub_comments = max_sub_comments
        self.min_sleep = min_sleep
        self.max_sleep = max_sleep
        self.comment_sleep = comment_sleep
        self.max_concurrency = max_concurrency
        self.browser_path = browser_path
        self.inter_note_sleep = inter_note_sleep
        self.publish_date_after = validate_comment_publish_date_after(
            publish_date_after
        )
        self.last_exit_code = 0
        self.conn = sqlite3.connect(self.db_path, timeout=30)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        self.conn.close()

    @staticmethod
    def _stop_child(process) -> None:
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    def saved_comment_count(self, note_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM xhs_note_comment WHERE note_id = ?",
            (note_id,),
        ).fetchone()[0]

    def get_post(self, note_id: str) -> Dict:
        # The registry-selected database is the isolation boundary.  The
        # crawler_account column is historical provenance and may name the
        # first writer of a globally unique note/comment, so it must not hide
        # rows inside the selected database.
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
            "--max_concurrency_num", str(self.max_concurrency),
            "--headless", "yes",
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
            prepared.append((task_post, before, url))

        if not prepared:
            self.last_exit_code = 1
            return False

        command = self.crawler_command([item[2] for item in prepared])
        printable = " ".join(json.dumps(part, ensure_ascii=False) for part in command)
        print(f"[command] {printable}", file=log_file, flush=True)
        if self.dry_run:
            self.last_exit_code = 0
            print("[dry-run] crawler was not started", file=log_file, flush=True)
            for task_post, before, _ in prepared:
                update_post_status(task_id, task_post["note_id"], "pending", before, None, before)
            return True

        process = None
        terminal_note_ids = set()
        try:
            prepared_by_id = {
                item[0]["note_id"]: (item[0], item[1]) for item in prepared
            }
            process = subprocess.Popen(
                command,
                cwd=MEDIACRAWLER_ROOT,
                env=self.crawler_environment(task_id),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="", file=log_file, flush=True)
                start_match = re.search(r"\[specified-note-start\] note_id=([^\s]+)", line)
                complete_match = re.search(r"\[specified-note-complete\] note_id=([^\s]+)", line)
                failed_match = re.search(r"\[specified-note-failed\] note_id=([^\s]+)", line)
                if start_match and start_match.group(1) in prepared_by_id:
                    note_id = start_match.group(1)
                    _, before = prepared_by_id[note_id]
                    update_post_status(task_id, note_id, "running", before, None, before)
                if complete_match and complete_match.group(1) in prepared_by_id:
                    note_id = complete_match.group(1)
                    _, before = prepared_by_id[note_id]
                    after = self.saved_comment_count(note_id)
                    update_post_status(task_id, note_id, "completed", after, None, before)
                    terminal_note_ids.add(note_id)
                if failed_match and failed_match.group(1) in prepared_by_id:
                    note_id = failed_match.group(1)
                    _, before = prepared_by_id[note_id]
                    after = self.saved_comment_count(note_id)
                    update_post_status(
                        task_id, note_id, "failed", after,
                        "crawler failed while processing note", before,
                    )
                    terminal_note_ids.add(note_id)
            returncode = process.wait()
        except KeyboardInterrupt:
            self._stop_child(process)
            for task_post, before, _ in prepared:
                note_id = task_post["note_id"]
                if note_id in terminal_note_ids:
                    continue
                after = self.saved_comment_count(note_id)
                update_post_status(
                    task_id, note_id, "failed", after, "crawler interrupted", before
                )
            raise
        except Exception as exc:
            self._stop_child(process)
            returncode = 1
            print(f"[crawler-launch-error] {exc}", file=log_file, flush=True)

        self.last_exit_code = returncode
        success = returncode == 0
        for task_post, before, _ in prepared:
            note_id = task_post["note_id"]
            if note_id in terminal_note_ids:
                continue
            after = self.saved_comment_count(note_id)
            error = (
                "crawler exited without a per-note completion marker"
                if success else f"crawler exited with code {returncode}"
            )
            update_post_status(
                task_id, note_id, "failed",
                after, error, before,
            )
            print(
                f"[failed] {note_id}: "
                f"before={before} after={after} added={max(0, after - before)}",
                file=log_file,
                flush=True,
            )
        return success

    def crawler_environment(self, task_id: str = "") -> Dict[str, str]:
        """Use one explicitly configured isolated browser, never CDP."""
        env = os.environ.copy()
        env["MEDIACRAWLER_ENABLE_CDP"] = "false"
        env["MEDIACRAWLER_REQUIRE_CDP"] = "false"
        env["MEDIACRAWLER_AUTO_CLOSE_BROWSER"] = "true"
        env["MEDIACRAWLER_CDP_CONNECT_EXISTING"] = "false"
        env.pop("MEDIACRAWLER_CDP_ENDPOINT", None)
        env["MEDIACRAWLER_BROWSER_PATH"] = validate_standard_browser_path(
            self.browser_path
        )
        env["MEDIACRAWLER_ACCOUNT"] = self.account_id
        env["MEDIACRAWLER_SQLITE_DB_PATH"] = self.db_path
        env["MEDIACRAWLER_USER_DATA_DIR"] = self.user_data_dir
        env["MEDIACRAWLER_TASK_ID"] = task_id
        env["MEDIACRAWLER_LOG_PATH"] = str(
            LOG_ROOT
            / "dashboard"
            / "accounts"
            / self.account_id
            / f"{task_id or 'comment'}-runtime.log"
        )
        env["MEDIACRAWLER_ENABLE_RANDOM_SLEEP"] = "true"
        env["MEDIACRAWLER_CRAWLER_MIN_SLEEP_SEC"] = str(self.min_sleep)
        env["MEDIACRAWLER_CRAWLER_MAX_SLEEP_SEC"] = str(self.max_sleep)
        env["MEDIACRAWLER_CRAWLER_COMMENT_SLEEP_SEC"] = str(self.comment_sleep)
        env["MEDIACRAWLER_XHS_INTER_NOTE_SLEEP_SEC"] = str(self.inter_note_sleep)
        env["MEDIACRAWLER_XHS_NOTE_PUBLISH_DATE_AFTER"] = self.publish_date_after
        env["MEDIACRAWLER_MAX_CONCURRENCY_NUM"] = str(self.max_concurrency)
        return env


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a comment supplement task")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--account-id")
    parser.add_argument("--db-path", default=str(LEGACY_CONTENT_DB))
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--max-comments", type=int)
    parser.add_argument("--max-sub-comments", type=int)
    parser.add_argument("--delay", type=float)
    parser.add_argument("--min-sleep", type=int)
    parser.add_argument("--max-sleep", type=int)
    parser.add_argument("--comment-sleep", type=int)
    parser.add_argument("--max-concurrency", type=int)
    parser.add_argument("--browser-path")
    parser.add_argument("--publish-date-after")
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
    run_id = str(os.getenv("MEDIACRAWLER_RUN_ID", "") or "").strip()

    task = get_task(args.task_id)
    if not task:
        parser.error(f"task not found: {args.task_id}")
    try:
        task_config = json.loads(task.get("config_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        task_config = {}
    try:
        runtime_config, account = bind_task_config(
            task_config,
            account_id=args.account_id or task.get("account_id") or DEFAULT_ACCOUNT_ID,
            require_enabled=True,
        )
    except AccountRegistryError as exc:
        parser.error(str(exc))
    task_config = runtime_config
    args.account_id = account["account_id"]
    args.db_path = account["sqlite_db_path"]
    args.user_data_dir = account["user_data_dir"]
    args.batch_size = args.batch_size or int(
        task_config.get("batch_size", DEFAULT_COMMENT_TASK_BATCH_SIZE)
    )
    args.max_comments = args.max_comments or int(
        task_config.get("max_comments", DEFAULT_FIRST_LEVEL_COMMENTS)
    )
    if args.max_sub_comments is None:
        args.max_sub_comments = int(task_config.get("max_sub_comments", 200))
    if args.delay is None:
        args.delay = float(task_config.get("delay", DEFAULT_COMMENT_TASK_BATCH_DELAY))
    if args.min_sleep is None:
        args.min_sleep = int(task_config.get("min_sleep", COMMENTS_RATE.min_sleep))
    if args.max_sleep is None:
        args.max_sleep = int(task_config.get("max_sleep", COMMENTS_RATE.max_sleep))
    if args.comment_sleep is None:
        args.comment_sleep = int(task_config.get("comment_sleep", COMMENTS_RATE.comment_sleep))
    if args.max_concurrency is None:
        args.max_concurrency = int(task_config.get("max_concurrency", COMMENTS_RATE.max_concurrency))
    if args.get_sub_comments is None:
        args.get_sub_comments = bool(task_config.get("get_sub_comments", False))
    if args.browser_path is None:
        args.browser_path = str(task_config.get("browser_path", "") or "").strip()
    if args.publish_date_after is None:
        args.publish_date_after = task_config.get(
            "publish_date_after", DEFAULT_COMMENT_PUBLISH_DATE_AFTER
        )
    if (
        args.batch_size < 1
        or args.max_comments < 1
        or args.max_sub_comments < 0
        or args.min_sleep < 0
        or args.max_sleep < args.min_sleep
        or args.comment_sleep < 1
        or args.max_concurrency != 1
    ):
        parser.error("invalid task rate or count configuration")

    if os.getenv("MEDIACRAWLER_ENABLE_CDP", "false").lower() in ("1", "true", "yes"):
        parser.error("comment tasks require standard isolated browser mode; CDP is disabled")
    try:
        args.browser_path = validate_standard_browser_path(args.browser_path)
        args.publish_date_after = validate_comment_publish_date_after(
            args.publish_date_after
        )
    except ValueError as exc:
        parser.error(str(exc))

    posts = get_task_posts(args.task_id, status="failed" if args.retry_failed else "pending")
    if args.limit:
        posts = posts[:args.limit]
    if not posts:
        logger.info('No pending posts to process')
        finish_task(args.task_id, 0)
        finish_task_run(run_id, exit_code=0, status="completed")
        return 0

    db_path = args.db_path
    if not os.path.isabs(db_path):
        db_path = str(MEDIACRAWLER_ROOT / db_path)
    log_dir = DASHBOARD_DIR / "logs" / "accounts" / args.account_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{args.task_id}-{int(time.time())}.log"

    executor = CommentTaskExecutor(
        db_path, args.max_comments, args.dry_run, args.user_data_dir,
        args.get_sub_comments, args.max_sub_comments,
        args.min_sleep, args.max_sleep, args.comment_sleep, args.max_concurrency,
        args.browser_path, args.delay, args.publish_date_after,
        account_id=args.account_id,
    )
    if not args.dry_run:
        if task["status"] != "starting":
            executor.close()
            parser.error(f"task is {task['status']}, expected starting")
        try:
            assert_launch_reserved(args.task_id, "comment", account["user_data_dir"])
        except RuntimeError as exc:
            executor.close()
            parser.error(str(exc))
        start_task(args.task_id, str(log_path))
        mark_task_run_running(run_id, worker_pid=os.getpid())
    all_ok = True
    interrupted = False
    risk_control_stopped = False
    fatal_error = None
    profile_guard = (
        nullcontext()
        if args.dry_run
        else acquire_profile_lock(
            account_id=account["account_id"],
            user_data_dir=account["user_data_dir"],
            task_id=args.task_id,
        )
    )
    lease_guard = (
        nullcontext()
        if args.dry_run
        else launch_lease_heartbeat(args.task_id, account["user_data_dir"])
    )
    try:
        with profile_guard, lease_guard:
            if not args.dry_run:
                confirm_launch(args.task_id, account["user_data_dir"])
            with log_path.open("a", encoding="utf-8") as log_file:
                print(
                    f"[run] run_id={run_id or '<legacy-untracked>'} "
                    f"profile_id={os.getenv('MEDIACRAWLER_PROFILE_ID', '')} "
                    f"store_id={os.getenv('MEDIACRAWLER_STORE_ID', '')}",
                    file=log_file,
                    flush=True,
                )
                print(f"[task-run] posts={len(posts)} browser_launches=1", file=log_file, flush=True)
                all_ok = executor.run_batch(args.task_id, posts, log_file)
                if executor.last_exit_code == RISK_CONTROL_EXIT_CODE:
                    risk_control_stopped = True
                    error = "XHS risk control CAPTCHA (HTTP 461/471); remaining notes were not started"
                    print(f"[risk-control-stop] {error}", file=log_file, flush=True)
                    for pending_post in get_task_posts(args.task_id, status="pending"):
                        note_id = pending_post["note_id"]
                        count = executor.saved_comment_count(note_id)
                        update_post_status(
                            args.task_id,
                            note_id,
                            "failed",
                            count,
                            error,
                            pending_post["comment_count_before"],
                        )
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

    final_exit_code = 130 if interrupted else (RISK_CONTROL_EXIT_CODE if risk_control_stopped else (0 if all_ok else 1))
    finish_task(args.task_id, final_exit_code)
    final_task = get_task(args.task_id) or {}
    task_status = final_task.get("status")
    run_status = {
        "completed": "completed",
        "completed_with_errors": "completed_with_errors",
        "cancelled": "cancelled",
    }.get(task_status, "failed" if final_exit_code else "completed")
    finish_task_run(
        run_id,
        exit_code=final_exit_code,
        status=run_status,
        stop_reason=fatal_error or (
            "XHS risk control CAPTCHA (HTTP 461/471)"
            if risk_control_stopped
            else ("task interrupted" if interrupted else "")
        ),
    )
    if not args.dry_run:
        record_completion(
            task_id=args.task_id,
            task_kind="comment",
            user_data_dir=account["user_data_dir"],
            exit_code=final_exit_code,
        )
    logger.info(f"Task {args.task_id} {('finished' if all_ok else 'finished with errors')}; log={log_path}")
    return final_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
