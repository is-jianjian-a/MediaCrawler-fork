"""Cross-account worker routing tests; no browser or network is started."""

import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from dashboard import comment_fetcher, crawl_runner, server
from dashboard.comment_fetcher import CommentTaskExecutor
from dashboard.crawl_runner import _build_command


def _browser(tmp_path: Path) -> str:
    path = tmp_path / "isolated-chromium"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_search_worker_explicitly_overrides_parent_account_route(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIACRAWLER_ACCOUNT", "wrong-parent")
    monkeypatch.setenv("MEDIACRAWLER_SQLITE_DB_PATH", "/wrong/parent.db")
    monkeypatch.setenv("MEDIACRAWLER_USER_DATA_DIR", "%s_wrong_parent")
    monkeypatch.setenv("MEDIACRAWLER_ENABLE_CDP", "true")
    browser = _browser(tmp_path)
    _, env = _build_command(
        {
            "id": "crawl-account-a",
            "keywords": ["test"],
            "config": {
                "account_id": "A",
                "sqlite_db_path": str(tmp_path / "a.db"),
                "user_data_dir": "%s_user_data_dir_accountA",
                "browser_path": browser,
                "enable_cdp": False,
                "require_cdp": False,
            },
        }
    )

    assert env["MEDIACRAWLER_ACCOUNT"] == "A"
    assert env["MEDIACRAWLER_SQLITE_DB_PATH"] == str(tmp_path / "a.db")
    assert env["MEDIACRAWLER_USER_DATA_DIR"] == "%s_user_data_dir_accountA"
    assert env["MEDIACRAWLER_BROWSER_PATH"] == browser
    assert env["MEDIACRAWLER_ENABLE_CDP"] == "false"
    assert env["MEDIACRAWLER_AUTO_CLOSE_BROWSER"] == "true"
    assert env["MEDIACRAWLER_LOG_PATH"] == str(
        crawl_runner.LOG_DIR / "accounts/A/crawl-account-a-runtime.log"
    )


def test_search_accounts_have_distinct_db_profile_and_log_routes(tmp_path):
    browser = _browser(tmp_path)

    def environment(account_id: str):
        return _build_command(
            {
                "id": f"crawl-{account_id}",
                "keywords": ["test"],
                "config": {
                    "account_id": account_id,
                    "sqlite_db_path": str(tmp_path / account_id / "content.db"),
                    "user_data_dir": f"%s_user_data_dir_account{account_id}",
                    "browser_path": browser,
                },
            }
        )[1]

    env_a = environment("A")
    env_b = environment("B")
    for key in (
        "MEDIACRAWLER_ACCOUNT",
        "MEDIACRAWLER_SQLITE_DB_PATH",
        "MEDIACRAWLER_USER_DATA_DIR",
        "MEDIACRAWLER_LOG_PATH",
    ):
        assert env_a[key] != env_b[key]


def test_comment_worker_uses_its_own_account_route(monkeypatch, tmp_path):
    browser = _browser(tmp_path)
    monkeypatch.setenv("MEDIACRAWLER_ACCOUNT", "wrong-parent")
    monkeypatch.setenv("MEDIACRAWLER_SQLITE_DB_PATH", "/wrong/parent.db")
    db_path = tmp_path / "B" / "content.db"
    db_path.parent.mkdir()
    executor = CommentTaskExecutor(
        str(db_path),
        max_comments=1,
        dry_run=True,
        user_data_dir="%s_user_data_dir_accountB",
        browser_path=browser,
        account_id="B",
    )
    try:
        env = executor.crawler_environment("comment-b")
    finally:
        executor.close()

    assert env["MEDIACRAWLER_ACCOUNT"] == "B"
    assert env["MEDIACRAWLER_SQLITE_DB_PATH"] == str(db_path)
    assert env["MEDIACRAWLER_USER_DATA_DIR"] == "%s_user_data_dir_accountB"
    assert env["MEDIACRAWLER_LOG_PATH"] == str(
        comment_fetcher.LOG_ROOT / "dashboard/accounts/B/comment-b-runtime.log"
    )
    assert env["MEDIACRAWLER_CDP_CONNECT_EXISTING"] == "false"
    assert env["MEDIACRAWLER_AUTO_CLOSE_BROWSER"] == "true"


def test_health_uses_recent_task_account_database(monkeypatch, tmp_path):
    account_db = tmp_path / "accounts" / "A" / "content.db"
    account_db.parent.mkdir(parents=True)
    conn = sqlite3.connect(account_db)
    conn.executescript(
        """
        CREATE TABLE xhs_note (note_id TEXT PRIMARY KEY, add_ts INTEGER);
        CREATE TABLE xhs_note_comment (comment_id TEXT PRIMARY KEY, add_ts INTEGER);
        INSERT INTO xhs_note VALUES ('latest', 2000000000000);
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(server, "account_db_path", lambda account_id: str(account_db))
    monkeypatch.setattr(
        server,
        "get_crawl_task_health",
        lambda: {
            "status": "idle",
            "label": "当前无运行任务",
            "active_task": None,
            "recent_task": {"id": "crawl-a", "account_id": "A", "status": "completed"},
        },
    )
    monkeypatch.setattr(
        server, "get_process_info", lambda: (False, None, None, None, "unknown")
    )

    response = server.app.test_client().get("/api/health")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["account_id"] == "A"
    assert payload["db_connected"] is True
    assert payload["storage"]["latest_write_ms"] == 2000000000000
    assert payload["storage"]["quick_check"] == "not_run"


def test_account_api_does_not_expose_local_routing_paths():
    account = {
        "account_id": "A",
        "display_name": "Research A",
        "enabled": True,
        "storage_mode": "dedicated",
        "profile_exists": True,
        "content_db_exists": True,
        "user_data_dir": "%s_user_data_dir_accountA",
        "profile_path": "/secret/profile-a",
        "sqlite_db_path": "/secret/content-a.db",
        "browser_path": "/secret/chromium",
    }
    with (
        mock.patch.object(server, "list_accounts", return_value=[account]),
        mock.patch.object(server, "get_risk_policy_status", return_value={"state": "normal"}),
        mock.patch.object(server, "native_profile_owner", return_value={}),
    ):
        response = server.app.test_client().get("/api/xhs-accounts")

    payload = response.get_json()["accounts"][0]
    assert payload["account_id"] == "A"
    assert "profile_path" not in payload
    assert "sqlite_db_path" not in payload
    assert "browser_path" not in payload


def test_profile_busy_response_is_safe_and_does_not_reserve_launch():
    account = {"user_data_dir": "%s_user_data_dir_accountA"}
    with (
        server.app.test_request_context(),
        mock.patch.object(
            server,
            "native_profile_owner",
            return_value={"in_use": True, "pid": 123, "owner": "private-lock"},
        ),
    ):
        response, status = server._profile_busy_response(account)

    payload = response.get_json()
    assert status == 409
    assert payload["profile_in_use"] is True
    assert payload["owner_pid"] == 123
    assert "private-lock" not in str(payload)


def test_legacy_content_route_is_opened_read_only(monkeypatch, tmp_path):
    legacy_db = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy_db)
    conn.execute("CREATE TABLE evidence (value TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(server, "_crawler_db_path", str(legacy_db))

    conn = server._with_crawler_db()
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO evidence VALUES ('unexpected')")
    finally:
        conn.close()


def test_index_maintenance_skips_disabled_legacy_routes():
    enabled = {"account_id": "A", "enabled": True}
    disabled = {"account_id": "legacy-default", "enabled": False}
    connection = mock.MagicMock()
    connection.execute.return_value.fetchone.return_value = None
    with (
        mock.patch.object(server, "list_accounts", return_value=[enabled]) as accounts,
        mock.patch.object(server, "_with_crawler_db", return_value=connection) as connect,
    ):
        server.ensure_crawler_db_indexes()

    accounts.assert_called_once_with(include_disabled=False)
    connect.assert_called_once_with("A", writable=True)
    connection.close.assert_called_once()
