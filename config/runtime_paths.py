"""User data, application state, and log paths for MediaCrawler."""

from __future__ import annotations

import os
from pathlib import Path


def _path_from_env(name: str, default: Path) -> Path:
    return Path(os.getenv(name, str(default))).expanduser().resolve(strict=False)


DATA_ROOT = _path_from_env(
    "MEDIACRAWLER_DATA_ROOT",
    Path.home() / "data" / "datasets" / "mediacrawler",
)
STATE_ROOT = _path_from_env(
    "MEDIACRAWLER_STATE_ROOT",
    Path.home() / "Library" / "Application Support" / "MediaCrawler",
)
LOG_ROOT = _path_from_env(
    "MEDIACRAWLER_LOG_ROOT",
    Path.home() / "Library" / "Logs" / "MediaCrawler",
)

CONTENT_DB_ROOT = DATA_ROOT / "accounts"
LEGACY_CONTENT_DB = DATA_ROOT / "sqlite_tables.db"
CONTENT_ARCHIVE_ROOT = DATA_ROOT / "legacy-archive"
EXPORT_ROOT = DATA_ROOT / "exports"

CONTROL_DB_ROOT = STATE_ROOT / "database"
TASK_DB = CONTROL_DB_ROOT / "task_manager.db"
DASHBOARD_DB = CONTROL_DB_ROOT / "dashboard.db"
CRAWL_TASK_DB = CONTROL_DB_ROOT / "crawl_task_manager.db"
CONTROL_ARCHIVE_ROOT = CONTROL_DB_ROOT / "archive"
BROWSER_DATA_ROOT = STATE_ROOT / "browser-data"
