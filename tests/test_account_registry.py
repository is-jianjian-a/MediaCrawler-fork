"""Account registry and physical profile isolation tests."""

import os
import socket
import sqlite3
from pathlib import Path

import pytest

from dashboard import account_registry as registry
from dashboard import profile_lock


@pytest.fixture
def isolated_registry(monkeypatch, tmp_path):
    task_db = tmp_path / "dashboard" / "task_manager.db"
    browser_root = tmp_path / "browser_data"
    account_root = tmp_path / "database" / "accounts"
    legacy_db = tmp_path / "database" / "sqlite_tables.db"
    monkeypatch.setattr(registry, "TASK_DB", task_db)
    monkeypatch.setattr(registry, "BROWSER_DATA_ROOT", browser_root)
    monkeypatch.setattr(registry, "ACCOUNT_DATA_ROOT", account_root)
    monkeypatch.setattr(registry, "LEGACY_CONTENT_DB", legacy_db)
    monkeypatch.setattr(profile_lock, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.delenv("MEDIACRAWLER_BROWSER_PATH", raising=False)
    registry.init_account_registry_db()

    browser = tmp_path / "isolated-chromium"
    browser.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    browser.chmod(0o755)
    return browser


def test_accounts_get_distinct_profiles_and_databases(isolated_registry):
    account_a = registry.create_account(
        account_id="A", browser_path=str(isolated_registry)
    )
    account_b = registry.create_account(
        account_id="B", browser_path=str(isolated_registry)
    )

    assert account_a["profile_path"] != account_b["profile_path"]
    assert account_a["sqlite_db_path"] != account_b["sqlite_db_path"]
    assert Path(account_a["sqlite_db_path"]).parent.name == "A"
    assert Path(account_b["sqlite_db_path"]).parent.name == "B"


def test_runtime_binding_rejects_enabled_database_collision(isolated_registry):
    account_a = registry.create_account(
        account_id="A", browser_path=str(isolated_registry), enabled=True
    )
    registry.create_account(
        account_id="B", browser_path=str(isolated_registry), enabled=True
    )
    conn = sqlite3.connect(registry.TASK_DB)
    conn.execute(
        "UPDATE xhs_accounts SET sqlite_db_path=? WHERE account_id='B'",
        (account_a["sqlite_db_path"],),
    )
    conn.commit()
    conn.close()

    with pytest.raises(registry.AccountRegistryError, match="content database conflicts"):
        registry.bind_task_config({}, account_id="A")


def test_duplicate_physical_profile_alias_is_rejected(isolated_registry):
    registry.create_account(
        account_id="A",
        browser_path=str(isolated_registry),
        user_data_dir="%s_user_data_dir_shared",
    )
    with pytest.raises(registry.AccountRegistryError, match="physical browser profile"):
        registry.create_account(
            account_id="B",
            browser_path=str(isolated_registry),
            user_data_dir="profiles/../%s_user_data_dir_shared",
        )


def test_account_id_case_alias_is_rejected(isolated_registry):
    registry.create_account(account_id="Research", browser_path=str(isolated_registry))
    with pytest.raises(registry.AccountRegistryError, match="already registered"):
        registry.create_account(account_id="research", browser_path=str(isolated_registry))


def test_default_legacy_database_migrates_to_private_working_copy(isolated_registry):
    registry.LEGACY_CONTENT_DB.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(registry.LEGACY_CONTENT_DB)
    source.execute("CREATE TABLE evidence (value TEXT)")
    source.execute("INSERT INTO evidence VALUES ('kept')")
    source.commit()
    source.close()

    migrated = registry.migrate_default_account_to_dedicated()

    assert migrated["storage_mode"] == "dedicated"
    assert Path(migrated["sqlite_db_path"]) != registry.LEGACY_CONTENT_DB
    assert registry.LEGACY_CONTENT_DB.exists()
    copied = sqlite3.connect(migrated["sqlite_db_path"])
    assert copied.execute("SELECT value FROM evidence").fetchone()[0] == "kept"
    copied.close()


def test_system_chrome_is_always_rejected(monkeypatch, tmp_path):
    fake_system = tmp_path / "Google Chrome"
    fake_system.write_text("chrome", encoding="utf-8")
    fake_system.chmod(0o755)
    monkeypatch.setattr(registry, "SYSTEM_CHROME_PATH", fake_system)
    with pytest.raises(registry.AccountRegistryError, match="system Google Chrome"):
        registry.validate_browser_path(str(fake_system))


def test_profile_lock_blocks_same_profile_but_not_another(isolated_registry):
    with profile_lock.acquire_profile_lock(
        account_id="A",
        user_data_dir="%s_user_data_dir_accountA",
        task_id="task-a",
    ):
        with pytest.raises(profile_lock.ProfileLockError, match="task-a"):
            with profile_lock.acquire_profile_lock(
                account_id="A",
                user_data_dir="%s_user_data_dir_accountA",
                task_id="task-a-racer",
            ):
                pass
        with profile_lock.acquire_profile_lock(
            account_id="B",
            user_data_dir="%s_user_data_dir_accountB",
            task_id="task-b",
        ):
            pass


def test_profile_lock_detects_manually_open_chromium(isolated_registry):
    user_data_dir = "%s_user_data_dir_accountA"
    profile_path = registry.resolve_profile_path(user_data_dir)
    profile_path.mkdir(parents=True)
    (profile_path / "SingletonLock").symlink_to(
        f"{socket.gethostname()}-{os.getpid()}"
    )

    owner = profile_lock.native_profile_owner(user_data_dir)
    assert owner["in_use"] is True
    assert owner["pid"] == os.getpid()
    with pytest.raises(profile_lock.ProfileLockError, match="already open"):
        with profile_lock.acquire_profile_lock(
            account_id="A", user_data_dir=user_data_dir, task_id="task-a"
        ):
            pass


def test_locking_one_account_database_does_not_block_another(isolated_registry):
    account_a = registry.create_account(
        account_id="A", browser_path=str(isolated_registry)
    )
    account_b = registry.create_account(
        account_id="B", browser_path=str(isolated_registry)
    )
    for account in (account_a, account_b):
        conn = sqlite3.connect(account["sqlite_db_path"])
        conn.execute("CREATE TABLE isolated_write (value TEXT)")
        conn.commit()
        conn.close()

    locked = sqlite3.connect(account_a["sqlite_db_path"], timeout=0)
    locked.execute("BEGIN EXCLUSIVE")
    try:
        independent = sqlite3.connect(account_b["sqlite_db_path"], timeout=0)
        independent.execute("INSERT INTO isolated_write VALUES ('ok')")
        independent.commit()
        assert independent.execute(
            "SELECT value FROM isolated_write"
        ).fetchone()[0] == "ok"
        independent.close()
    finally:
        locked.rollback()
        locked.close()


def test_historical_task_backfill_does_not_guess_default_account(
    monkeypatch, tmp_path
):
    task_db = tmp_path / "dashboard" / "task_manager.db"
    task_db.parent.mkdir(parents=True)
    conn = sqlite3.connect(task_db)
    conn.execute(
        """CREATE TABLE crawl_tasks (
               id TEXT PRIMARY KEY, config_json TEXT, created_at REAL, status TEXT
           )"""
    )
    conn.execute(
        "INSERT INTO crawl_tasks VALUES ('legacy', '{}', 1, 'completed')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(registry, "TASK_DB", task_db)
    monkeypatch.setattr(registry, "BROWSER_DATA_ROOT", tmp_path / "browser_data")
    monkeypatch.setattr(registry, "ACCOUNT_DATA_ROOT", tmp_path / "accounts")
    monkeypatch.setattr(registry, "LEGACY_CONTENT_DB", tmp_path / "legacy.db")
    monkeypatch.delenv("MEDIACRAWLER_BROWSER_PATH", raising=False)

    registry.init_account_registry_db()
    conn = sqlite3.connect(task_db)
    account_id = conn.execute(
        "SELECT account_id FROM crawl_tasks WHERE id='legacy'"
    ).fetchone()[0]
    conn.close()
    assert account_id == "legacy-default"
