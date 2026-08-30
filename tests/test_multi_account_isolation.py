"""Cross-account worker routing tests; no browser or network is started."""

from pathlib import Path
from unittest import mock

from dashboard.comment_fetcher import CommentTaskExecutor
from dashboard.crawl_runner import _build_command
from dashboard import server


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
    assert env["MEDIACRAWLER_LOG_PATH"].endswith(
        "dashboard/logs/accounts/A/crawl-account-a-runtime.log"
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
    assert env["MEDIACRAWLER_LOG_PATH"].endswith(
        "dashboard/logs/accounts/B/comment-b-runtime.log"
    )
    assert env["MEDIACRAWLER_CDP_CONNECT_EXISTING"] == "false"
    assert env["MEDIACRAWLER_AUTO_CLOSE_BROWSER"] == "true"


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
