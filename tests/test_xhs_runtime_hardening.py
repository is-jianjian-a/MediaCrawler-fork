"""P0 XHS runtime hardening contracts.

These tests use only temporary control/content databases and never start a
browser or open a user database.
"""

import json
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from dashboard import account_registry as registry
from dashboard import comment_fetcher
from tools import browser_safety


@pytest.fixture
def isolated_registry(monkeypatch, tmp_path):
    """Point registry and normalized metadata at one disposable sandbox."""
    task_db = tmp_path / "state" / "task_manager.db"
    browser_root = tmp_path / "state" / "browser-data"
    account_root = tmp_path / "data" / "accounts"
    legacy_db = tmp_path / "data" / "sqlite_tables.db"
    archive_root = tmp_path / "data" / "legacy-archive"

    monkeypatch.setattr(registry, "TASK_DB", task_db)
    monkeypatch.setattr(registry, "BROWSER_DATA_ROOT", browser_root)
    monkeypatch.setattr(registry, "ACCOUNT_DATA_ROOT", account_root)
    monkeypatch.setattr(registry, "LEGACY_CONTENT_DB", legacy_db)
    monkeypatch.setattr(registry, "CONTENT_ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(registry, "_INITIALIZED_DATABASES", set())
    monkeypatch.setattr(registry, "DEFAULT_ACCOUNT_ID", "02")
    monkeypatch.setattr(
        registry, "DEFAULT_USER_DATA_DIR", "%s_user_data_dir_account02"
    )

    browser = tmp_path / "isolated-chromium"
    browser.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    browser.chmod(0o755)
    monkeypatch.setenv("MEDIACRAWLER_BROWSER_PATH", str(browser))
    monkeypatch.delenv("MEDIACRAWLER_RUN_ID", raising=False)
    return {
        "task_db": task_db,
        "browser_root": browser_root,
        "account_root": account_root,
        "legacy_db": legacy_db,
        "archive_root": archive_root,
        "browser": browser,
    }


def _enable_registry_account(account_id: str) -> None:
    conn = sqlite3.connect(registry.TASK_DB)
    try:
        conn.execute(
            "UPDATE xhs_accounts SET enabled=1 WHERE account_id=?", (account_id,)
        )
        conn.commit()
    finally:
        conn.close()


def test_account_id_case_alias_is_rejected(isolated_registry):
    registry.init_account_registry_db()
    registry.create_account(account_id="A", browser_path=str(isolated_registry["browser"]))

    with pytest.raises(registry.AccountRegistryError, match="already registered"):
        registry.create_account(
            account_id="a", browser_path=str(isolated_registry["browser"])
        )


def test_default_user_data_dir_is_derived_from_default_account_id(
    isolated_registry, monkeypatch
):
    monkeypatch.setattr(registry, "DEFAULT_ACCOUNT_ID", "Primary_A")
    registry.init_account_registry_db()

    account = registry.get_account(registry.DEFAULT_ACCOUNT_ID)
    assert account is not None
    assert account["user_data_dir"] == registry.default_user_data_dir(
        registry.DEFAULT_ACCOUNT_ID
    )


def test_non_dry_run_binding_rejects_legacy_shared_write_route(isolated_registry):
    registry.init_account_registry_db()
    _enable_registry_account("02")

    with pytest.raises(
        registry.AccountRegistryError, match="legacy shared storage.*read-only"
    ):
        registry.bind_task_config(
            {"dry_run": False}, account_id="02", require_enabled=True
        )


def test_non_dry_run_search_launch_rejects_legacy_shared_route(
    isolated_registry, monkeypatch
):
    registry.init_account_registry_db()
    _enable_registry_account("02")

    from dashboard import crawl_runner

    monkeypatch.setattr(
        crawl_runner,
        "get_crawl_task",
        lambda task_id: {
            "id": task_id,
            "account_id": "02",
            "status": "starting",
            "keywords": ["test"],
            "config": {"dry_run": False},
        },
    )
    finished = mock.Mock()
    monkeypatch.setattr(crawl_runner, "finish_crawl_task", finished)
    popen = mock.Mock()
    monkeypatch.setattr(crawl_runner.subprocess, "Popen", popen)

    assert crawl_runner.run_task("crawl-legacy") == 1
    finished.assert_called_once()
    popen.assert_not_called()


def test_legacy_catalog_and_historical_placeholder_are_read_only(
    isolated_registry,
):
    legacy_db = isolated_registry["legacy_db"]
    legacy_db.parent.mkdir(parents=True, exist_ok=True)
    legacy_conn = sqlite3.connect(legacy_db)
    legacy_conn.execute(
        "CREATE TABLE xhs_note (note_id TEXT PRIMARY KEY, title TEXT)"
    )
    legacy_conn.execute("INSERT INTO xhs_note VALUES ('legacy-note', 'legacy')")
    legacy_conn.commit()
    legacy_conn.close()

    task_db = isolated_registry["task_db"]
    task_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(task_db)
    conn.execute(
        """CREATE TABLE crawl_tasks (
               id TEXT PRIMARY KEY,
               config_json TEXT,
               created_at REAL,
               status TEXT
           )"""
    )
    conn.execute(
        "INSERT INTO crawl_tasks VALUES (?, ?, ?, ?)",
        (
            "legacy-crawl",
            json.dumps({"user_data_dir": "%s_historical_profile"}),
            1.0,
            "completed",
        ),
    )
    conn.commit()
    conn.close()

    registry.init_account_registry_db()

    from dashboard import xhs_data_model

    sources = xhs_data_model.list_catalog_sources(
        task_db=task_db, existing_only=False
    )
    legacy_source = next(
        source for source in sources if source["store_kind"] == "legacy_aggregate"
    )
    assert legacy_source["read_only"] == 1

    catalog = xhs_data_model.open_content_catalog(task_db=task_db)
    try:
        assert catalog.execute(
            "SELECT title FROM xhs_note WHERE note_id='legacy-note'"
        ).fetchone()[0] == "legacy"
        with pytest.raises(sqlite3.OperationalError, match="view"):
            catalog.execute("INSERT INTO xhs_note VALUES ('blocked', 'write')")
    finally:
        catalog.close()

    placeholder = next(
        account
        for account in registry.list_accounts()
        if account.get("record_kind") == "historical_placeholder"
    )
    metadata = xhs_data_model.route_metadata(
        placeholder["account_id"], task_db=task_db
    )
    assert metadata["store_kind"] == "legacy_aggregate"
    assert metadata["read_only"] == 1

    conn = sqlite3.connect(task_db)
    try:
        route = conn.execute(
            "SELECT route_role, access_mode FROM xhs_store_routes WHERE account_id=?",
            (placeholder["account_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert route == ("historical", "read_only")


def test_system_google_chrome_is_rejected_by_all_browser_entry_points(
    monkeypatch, tmp_path
):
    system_chrome = (
        tmp_path
        / "Applications"
        / "Google Chrome.app"
        / "Contents"
        / "MacOS"
        / "Google Chrome"
    )
    system_chrome.parent.mkdir(parents=True)
    system_chrome.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    system_chrome.chmod(0o755)
    monkeypatch.setattr(browser_safety, "SYSTEM_GOOGLE_CHROME", system_chrome)

    with pytest.raises(browser_safety.BrowserPathError, match="system Google Chrome"):
        browser_safety.validate_automation_browser_path(str(system_chrome))
    with pytest.raises(registry.AccountRegistryError, match="system Google Chrome"):
        registry.validate_browser_path(str(system_chrome))
    with pytest.raises(ValueError, match="system Google Chrome"):
        comment_fetcher.validate_standard_browser_path(str(system_chrome))


def test_backup_sqlite_database_captures_active_wal_latest_row(
    monkeypatch, tmp_path
):
    from dashboard.sqlite_maintenance import backup_sqlite_database

    source = tmp_path / "source.db"
    destination = tmp_path / "backups" / "source-copy.db"
    writer = sqlite3.connect(source)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "CREATE TABLE records (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )
        writer.execute("INSERT INTO records VALUES (1, 'before-wal')")
        writer.commit()
        writer.execute("INSERT INTO records VALUES (2, 'latest-wal-row')")
        writer.commit()

        assert Path(f"{source}-wal").exists()
        backup_sqlite_database(source, destination)
    finally:
        writer.close()

    copied = sqlite3.connect(destination)
    try:
        assert copied.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copied.execute(
            "SELECT payload FROM records WHERE id=2"
        ).fetchone()[0] == "latest-wal-row"
    finally:
        copied.close()
