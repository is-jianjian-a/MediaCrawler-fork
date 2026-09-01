#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run a Dashboard keyword crawl task and persist its status."""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

MEDIACRAWLER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MEDIACRAWLER_ROOT))

from tools.app_runner import RISK_CONTROL_EXIT_CODE

try:
    from dashboard.crawl_task_manager import (
        finish_crawl_task,
        get_crawl_task,
        set_crawl_worker_pid,
        start_crawl_task,
    )
except ModuleNotFoundError:  # Support direct script execution.
    from crawl_task_manager import (  # type: ignore[no-redef]
        finish_crawl_task,
        get_crawl_task,
        set_crawl_worker_pid,
        start_crawl_task,
    )
try:
    from dashboard.rate_policy import DEFAULT_FIRST_LEVEL_COMMENTS, keyword_rate_defaults
except ModuleNotFoundError:  # Support direct script execution.
    from rate_policy import DEFAULT_FIRST_LEVEL_COMMENTS, keyword_rate_defaults  # type: ignore[no-redef]
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
import logging
logger = logging.getLogger("MediaCrawler")

from config.runtime_paths import LOG_ROOT


DASHBOARD_DIR = MEDIACRAWLER_ROOT / "dashboard"
LOG_DIR = LOG_ROOT / "dashboard"
MAX_START_PAGE = 1000


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _validated_start_page(value) -> int:
    try:
        start_page = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("start_page must be an integer") from exc
    if not 1 <= start_page <= MAX_START_PAGE:
        raise ValueError(f"start_page must be between 1 and {MAX_START_PAGE}")
    return start_page


def _stop_child(process) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _build_command(task: dict) -> tuple[list[str], dict]:
    task_id = task.get("id") or ""
    config = dict(task.get("config") or {})
    if os.getenv("MEDIACRAWLER_RISK_CANARY", "false").lower() in ("1", "true", "yes"):
        config.update(
            {
                "max_count": min(5, max(1, int(config.get("max_count", 5) or 5))),
                "get_comments": False,
                "get_sub_comments": False,
                "max_concurrency": 1,
                "min_sleep": 240,
                "max_sleep": 300,
                "pre_search_delay": 75,
            }
        )
    keywords = task.get("keywords") or []
    stop_condition = str(config.get("stop_condition", "new_count") or "new_count")
    get_comments = bool(config.get("get_comments"))
    start_page = _validated_start_page(config.get("start_page", 1))
    rate_defaults = keyword_rate_defaults(get_comments)
    search_max_items = max(0, int(config.get("max_count", 0 if stop_condition == "date_floor" else 100) or 0))
    crawler_max_count = 1_000_000 if stop_condition == "date_floor" else max(1, search_max_items)
    uv = shutil.which("uv")
    command = ([uv, "run"] if uv else [sys.executable]) + [
        "main.py",
        "--platform",
        "xhs",
        "--type",
        "search",
        "--save_data_option",
        "sqlite",
        "--keywords",
        ",".join(keywords),
        "--max_count",
        str(crawler_max_count),
        "--get_comment",
        "yes" if get_comments else "no",
        "--get_sub_comment",
        "yes" if config.get("get_sub_comments") else "no",
        "--max_comments_count_singlenotes",
        str(config.get("max_comments", DEFAULT_FIRST_LEVEL_COMMENTS)),
        "--max_sub_comments_count_singlenotes",
        str(config.get("max_sub_comments", 10)),
        "--max_concurrency_num",
        str(config.get("max_concurrency", 1)),
        "--headless",
        "yes" if config.get("headless") else "no",
    ]

    env = os.environ.copy()
    account_id = str(config.get("account_id", DEFAULT_ACCOUNT_ID))
    runtime_log_path = (
        LOG_DIR / "accounts" / account_id / f"{task_id}-runtime.log"
    )
    env.update(
        {
            "MEDIACRAWLER_KEYWORDS": ",".join(keywords),
            "MEDIACRAWLER_TASK_ID": task_id,
            "MEDIACRAWLER_START_PAGE": str(start_page),
            "MEDIACRAWLER_CRAWLER_MAX_NOTES_COUNT": str(crawler_max_count),
            "MEDIACRAWLER_ENABLE_GET_COMMENTS": _bool_text(get_comments),
            "MEDIACRAWLER_ENABLE_GET_SUB_COMMENTS": _bool_text(bool(config.get("get_sub_comments"))),
            "MEDIACRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES": str(
                config.get("max_comments", DEFAULT_FIRST_LEVEL_COMMENTS)
            ),
            "MEDIACRAWLER_MAX_SUB_COMMENTS_COUNT_SINGLENOTES": str(config.get("max_sub_comments", 10)),
            "MEDIACRAWLER_MAX_CONCURRENCY_NUM": str(config.get("max_concurrency", 1)),
            "MEDIACRAWLER_XHS_SORT_TYPE": str(config.get("sort_type", "time_descending")),
            "MEDIACRAWLER_XHS_NOTE_TYPE": str(config.get("note_type", "all")),
            "MEDIACRAWLER_XHS_NOTE_PUBLISH_DATE_AFTER": str(config.get("publish_date_after", "")),
            "MEDIACRAWLER_XHS_STOP_WHEN_BEFORE_DATE": _bool_text(stop_condition == "date_floor"),
            "MEDIACRAWLER_XHS_SEARCH_MAX_ITEMS": str(search_max_items),
            "MEDIACRAWLER_SMART_CRAWLER_COUNT_MODE": str(config.get("count_mode", "incremental")),
            "MEDIACRAWLER_ENABLE_RANDOM_SLEEP": _bool_text(bool(config.get("enable_random_sleep", True))),
            "MEDIACRAWLER_CRAWLER_MIN_SLEEP_SEC": str(
                config.get("min_sleep", rate_defaults["min_sleep"])
            ),
            "MEDIACRAWLER_CRAWLER_MAX_SLEEP_SEC": str(
                config.get("max_sleep", rate_defaults["max_sleep"])
            ),
            "MEDIACRAWLER_CRAWLER_COMMENT_SLEEP_SEC": str(
                config.get("comment_sleep", rate_defaults["comment_sleep"])
            ),
            "MEDIACRAWLER_XHS_NOTE_DETAIL_TIMEOUT_SEC": str(config.get("note_detail_timeout", 75)),
            "MEDIACRAWLER_XHS_PRE_SEARCH_DELAY_SEC": str(config.get("pre_search_delay", 75)),
            "MEDIACRAWLER_USER_DATA_DIR": str(config.get("user_data_dir", "%s_user_data_dir_account02")),
            "MEDIACRAWLER_ACCOUNT": account_id,
            "MEDIACRAWLER_SQLITE_DB_PATH": str(config.get("sqlite_db_path", "")),
            "MEDIACRAWLER_LOG_PATH": str(runtime_log_path),
            "MEDIACRAWLER_ENABLE_CDP": _bool_text(bool(config.get("enable_cdp"))),
            "MEDIACRAWLER_REQUIRE_CDP": _bool_text(bool(config.get("require_cdp"))),
            "MEDIACRAWLER_CDP_DEBUG_PORT": os.getenv(
                "MEDIACRAWLER_TASK_CDP_DEBUG_PORT",
                str(config.get("cdp_debug_port", 9222)),
            ),
            "MEDIACRAWLER_AUTO_CLOSE_BROWSER": "true",
            "MEDIACRAWLER_CDP_CONNECT_EXISTING": "false",
        }
    )
    if config.get("browser_path"):
        env["MEDIACRAWLER_BROWSER_PATH"] = str(config.get("browser_path"))
    return command, env


def run_task(task_id: str) -> int:
    run_id = str(os.getenv("MEDIACRAWLER_RUN_ID", "") or "").strip()
    task = get_crawl_task(task_id)
    if not task:
        print(f"crawl task not found: {task_id}", file=sys.stderr)
        return 2

    process = None
    try:
        runtime_config, account = bind_task_config(
            task.get("config") or {},
            account_id=task.get("account_id") or DEFAULT_ACCOUNT_ID,
            require_enabled=True,
        )
    except AccountRegistryError as exc:
        finish_crawl_task(task_id, 1, str(exc))
        finish_task_run(
            run_id, exit_code=1, status="failed", stop_reason=str(exc)
        )
        return 1
    task = dict(task)
    task["config"] = runtime_config
    task["account_id"] = account["account_id"]
    if not runtime_config.get("dry_run"):
        if task.get("status") != "starting":
            finish_crawl_task(task_id, 1, "worker expected task status starting")
            finish_task_run(
                run_id,
                exit_code=1,
                status="failed",
                stop_reason="worker expected task status starting",
            )
            return 1
        try:
            assert_launch_reserved(task_id, "search", account["user_data_dir"])
        except RuntimeError as exc:
            finish_crawl_task(task_id, 1, str(exc))
            finish_task_run(
                run_id, exit_code=1, status="failed", stop_reason=str(exc)
            )
            return 1

    account_log_dir = LOG_DIR / "accounts" / account["account_id"]
    account_log_dir.mkdir(parents=True, exist_ok=True)
    log_path = account_log_dir / f"{task_id}.log"
    start_crawl_task(task_id, str(log_path), os.getpid())
    mark_task_run_running(run_id, worker_pid=os.getpid())

    command, env = _build_command(task)
    config = runtime_config
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with log_path.open("a", encoding="utf-8", errors="replace") as log:
            log.write(f"[{started_at}] Dashboard crawl task started: {task_id}\n")
            log.write("Command: " + " ".join(command) + "\n")
            log.write("Keywords: " + ",".join(task.get("keywords") or []) + "\n")
            log.write(f"Start page: {env['MEDIACRAWLER_START_PAGE']}\n")
            log.write(f"Run: {run_id or '<legacy-untracked>'}\n")
            log.write("Config: " + repr(config) + "\n\n")
            log.flush()
            if config.get("dry_run"):
                log.write("[dry-run] Command was not executed.\n")
                finish_crawl_task(task_id, 0)
                finish_task_run(run_id, exit_code=0, status="completed")
                return 0
            with acquire_profile_lock(
                account_id=account["account_id"],
                user_data_dir=account["user_data_dir"],
                task_id=task_id,
            ):
                confirm_launch(task_id, account["user_data_dir"])
                with launch_lease_heartbeat(task_id, account["user_data_dir"]):
                    process = subprocess.Popen(
                        command,
                        cwd=str(MEDIACRAWLER_ROOT),
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                    set_crawl_worker_pid(task_id, process.pid)
                    exit_code = process.wait()
            ended_at = time.strftime("%Y-%m-%d %H:%M:%S")
            log.write(f"\n[{ended_at}] crawler exited with code {exit_code}\n")
            if exit_code == RISK_CONTROL_EXIT_CODE:
                error = "XHS risk control CAPTCHA (HTTP 461/471); task stopped immediately"
            else:
                error = "" if exit_code == 0 else f"crawler exited with code {exit_code}"
            finish_crawl_task(
                task_id,
                exit_code,
                error,
            )
            final_task = get_crawl_task(task_id) or {}
            run_status = (
                "cancelled"
                if final_task.get("status") == "cancelled"
                else ("completed" if exit_code == 0 else "failed")
            )
            finish_task_run(
                run_id,
                exit_code=exit_code,
                status=run_status,
                stop_reason=error,
            )
            record_completion(
                task_id=task_id,
                task_kind="search",
                user_data_dir=account["user_data_dir"],
                exit_code=exit_code,
            )
            return exit_code
    except Exception as exc:
        _stop_child(process)
        # The task has already transitioned to running above.  Record a terminal
        # failure instead of using the starting-only failure path and leaving a
        # stale running task behind.
        finish_crawl_task(task_id, 1, str(exc))
        finish_task_run(
            run_id, exit_code=1, status="failed", stop_reason=str(exc)
        )
        record_completion(
            task_id=task_id,
            task_kind="search",
            user_data_dir=account["user_data_dir"],
            exit_code=1,
        )
        try:
            with log_path.open("a", encoding="utf-8", errors="replace") as log:
                log.write(f"\n[error] {exc}\n")
        except Exception:
            logger.exception(f"Unhandled exception in run_task()")
            pass
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Dashboard keyword crawl task")
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    raise SystemExit(run_task(args.task_id))


if __name__ == "__main__":
    main()
