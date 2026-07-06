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

from crawl_task_manager import (
    fail_crawl_task_start,
    finish_crawl_task,
    get_crawl_task,
    set_crawl_worker_pid,
    start_crawl_task,
)


MEDIACRAWLER_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = MEDIACRAWLER_ROOT / "dashboard"
LOG_DIR = DASHBOARD_DIR / "logs"


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _build_command(task: dict) -> tuple[list[str], dict]:
    task_id = task.get("id") or ""
    config = task.get("config") or {}
    keywords = task.get("keywords") or []
    stop_condition = str(config.get("stop_condition", "new_count") or "new_count")
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
        "yes" if config.get("get_comments") else "no",
        "--get_sub_comment",
        "yes" if config.get("get_sub_comments") else "no",
        "--max_comments_count_singlenotes",
        str(config.get("max_comments", 10)),
        "--max_sub_comments_count_singlenotes",
        str(config.get("max_sub_comments", 10)),
        "--max_concurrency_num",
        str(config.get("max_concurrency", 1)),
        "--headless",
        "yes" if config.get("headless") else "no",
    ]

    env = os.environ.copy()
    env.update(
        {
            "MEDIACRAWLER_KEYWORDS": ",".join(keywords),
            "MEDIACRAWLER_TASK_ID": task_id,
            "MEDIACRAWLER_CRAWLER_MAX_NOTES_COUNT": str(crawler_max_count),
            "MEDIACRAWLER_ENABLE_GET_COMMENTS": _bool_text(bool(config.get("get_comments"))),
            "MEDIACRAWLER_ENABLE_GET_SUB_COMMENTS": _bool_text(bool(config.get("get_sub_comments"))),
            "MEDIACRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES": str(config.get("max_comments", 10)),
            "MEDIACRAWLER_MAX_SUB_COMMENTS_COUNT_SINGLENOTES": str(config.get("max_sub_comments", 10)),
            "MEDIACRAWLER_MAX_CONCURRENCY_NUM": str(config.get("max_concurrency", 1)),
            "MEDIACRAWLER_XHS_SORT_TYPE": str(config.get("sort_type", "time_descending")),
            "MEDIACRAWLER_XHS_NOTE_TYPE": str(config.get("note_type", "all")),
            "MEDIACRAWLER_XHS_NOTE_PUBLISH_DATE_AFTER": str(config.get("publish_date_after", "")),
            "MEDIACRAWLER_XHS_STOP_WHEN_BEFORE_DATE": _bool_text(stop_condition == "date_floor"),
            "MEDIACRAWLER_XHS_SEARCH_MAX_ITEMS": str(search_max_items),
            "MEDIACRAWLER_SMART_CRAWLER_COUNT_MODE": str(config.get("count_mode", "incremental")),
            "MEDIACRAWLER_ENABLE_RANDOM_SLEEP": _bool_text(bool(config.get("enable_random_sleep", True))),
            "MEDIACRAWLER_CRAWLER_MIN_SLEEP_SEC": str(config.get("min_sleep", 20)),
            "MEDIACRAWLER_CRAWLER_MAX_SLEEP_SEC": str(config.get("max_sleep", 40)),
            "MEDIACRAWLER_CRAWLER_COMMENT_SLEEP_SEC": str(config.get("comment_sleep", 5)),
            "MEDIACRAWLER_XHS_NOTE_DETAIL_TIMEOUT_SEC": str(config.get("note_detail_timeout", 75)),
            "MEDIACRAWLER_USER_DATA_DIR": str(config.get("user_data_dir", "%s_user_data_dir_account02")),
            "MEDIACRAWLER_ENABLE_CDP": _bool_text(bool(config.get("enable_cdp"))),
            "MEDIACRAWLER_REQUIRE_CDP": _bool_text(bool(config.get("require_cdp"))),
            "MEDIACRAWLER_CDP_DEBUG_PORT": str(config.get("cdp_debug_port", 9222)),
            "MEDIACRAWLER_AUTO_CLOSE_BROWSER": "false",
            "MEDIACRAWLER_CDP_CONNECT_EXISTING": "true",
        }
    )
    if config.get("browser_path"):
        env["MEDIACRAWLER_BROWSER_PATH"] = str(config.get("browser_path"))
    return command, env


def run_task(task_id: str) -> int:
    task = get_crawl_task(task_id)
    if not task:
        print(f"crawl task not found: {task_id}", file=sys.stderr)
        return 2

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{task_id}.log"
    start_crawl_task(task_id, str(log_path), os.getpid())

    command, env = _build_command(task)
    config = task.get("config") or {}
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with log_path.open("a", encoding="utf-8", errors="replace") as log:
            log.write(f"[{started_at}] Dashboard crawl task started: {task_id}\n")
            log.write("Command: " + " ".join(command) + "\n")
            log.write("Keywords: " + ",".join(task.get("keywords") or []) + "\n")
            log.write("Config: " + repr(config) + "\n\n")
            log.flush()
            if config.get("dry_run"):
                log.write("[dry-run] Command was not executed.\n")
                finish_crawl_task(task_id, 0)
                return 0
            process = subprocess.Popen(
                command,
                cwd=str(MEDIACRAWLER_ROOT),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
            )
            set_crawl_worker_pid(task_id, process.pid)
            exit_code = process.wait()
            ended_at = time.strftime("%Y-%m-%d %H:%M:%S")
            log.write(f"\n[{ended_at}] crawler exited with code {exit_code}\n")
            finish_crawl_task(
                task_id,
                exit_code,
                "" if exit_code == 0 else f"crawler exited with code {exit_code}",
            )
            return exit_code
    except Exception as exc:
        fail_crawl_task_start(task_id, str(exc))
        try:
            with log_path.open("a", encoding="utf-8", errors="replace") as log:
                log.write(f"\n[error] {exc}\n")
        except Exception:
            pass
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Dashboard keyword crawl task")
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    raise SystemExit(run_task(args.task_id))


if __name__ == "__main__":
    main()
