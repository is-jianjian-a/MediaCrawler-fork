"""Contract tests for Dashboard keyword-task rate propagation."""

from contextlib import nullcontext

from dashboard import crawl_runner
from dashboard import comment_fetcher as _comment_fetcher  # Ensures direct-script imports resolve.
from dashboard.crawl_runner import _build_command
from dashboard.server import _normalize_crawl_config
from config.base_config import parse_start_page
from tools.app_runner import RISK_CONTROL_EXIT_CODE


def _option(command: list[str], name: str) -> str:
    return command[command.index(name) + 1]


def test_post_only_task_uses_safe_fallbacks():
    command, env = _build_command(
        {"id": "crawl-test", "keywords": ["test"], "config": {"get_comments": False}}
    )

    assert _option(command, "--get_comment") == "no"
    assert _option(command, "--max_comments_count_singlenotes") == "5"
    assert _option(command, "--max_concurrency_num") == "1"
    assert env["MEDIACRAWLER_CRAWLER_MIN_SLEEP_SEC"] == "45"
    assert env["MEDIACRAWLER_CRAWLER_MAX_SLEEP_SEC"] == "65"
    assert env["MEDIACRAWLER_CRAWLER_COMMENT_SLEEP_SEC"] == "30"


def test_comment_task_uses_slower_fallbacks():
    command, env = _build_command(
        {"id": "crawl-test", "keywords": ["test"], "config": {"get_comments": True}}
    )

    assert _option(command, "--get_comment") == "yes"
    assert env["MEDIACRAWLER_CRAWLER_MIN_SLEEP_SEC"] == "60"
    assert env["MEDIACRAWLER_CRAWLER_MAX_SLEEP_SEC"] == "75"
    assert env["MEDIACRAWLER_CRAWLER_COMMENT_SLEEP_SEC"] == "90"


def test_explicit_rate_configuration_is_preserved():
    _, env = _build_command(
        {
            "id": "crawl-test",
            "keywords": ["test"],
            "config": {
                "get_comments": True,
                "min_sleep": 120,
                "max_sleep": 180,
                "comment_sleep": 45,
            },
        }
    )

    assert env["MEDIACRAWLER_CRAWLER_MIN_SLEEP_SEC"] == "120"
    assert env["MEDIACRAWLER_CRAWLER_MAX_SLEEP_SEC"] == "180"
    assert env["MEDIACRAWLER_CRAWLER_COMMENT_SLEEP_SEC"] == "45"


def test_recovery_canary_is_clamped_by_execution_layer(monkeypatch):
    monkeypatch.setenv("MEDIACRAWLER_RISK_CANARY", "true")
    command, env = _build_command(
        {
            "id": "crawl-canary",
            "keywords": ["test"],
            "config": {
                "max_count": 20,
                "get_comments": True,
                "max_concurrency": 4,
                "min_sleep": 10,
                "max_sleep": 20,
            },
        }
    )
    assert _option(command, "--max_count") == "5"
    assert _option(command, "--get_comment") == "no"
    assert _option(command, "--max_concurrency_num") == "1"
    assert env["MEDIACRAWLER_CRAWLER_MIN_SLEEP_SEC"] == "240"
    assert env["MEDIACRAWLER_CRAWLER_MAX_SLEEP_SEC"] == "300"
    assert env["MEDIACRAWLER_XHS_PRE_SEARCH_DELAY_SEC"] == "75"


def test_start_page_four_is_saved_and_forwarded():
    config = _normalize_crawl_config({"start_page": 4, "get_comments": False})
    _, env = _build_command(
        {"id": "crawl-page-four", "keywords": ["test"], "config": config}
    )

    assert config["start_page"] == 4
    assert env["MEDIACRAWLER_START_PAGE"] == "4"
    assert parse_start_page("4") == 4


def test_new_dashboard_crawl_tasks_default_to_background_browser():
    config = _normalize_crawl_config({"get_comments": False})
    command, _ = _build_command(
        {"id": "crawl-headless", "keywords": ["test"], "config": config}
    )

    assert config["headless"] is True
    assert _option(command, "--headless") == "yes"


def test_invalid_start_page_is_rejected():
    for value in (0, -1, 1001, "not-a-page"):
        try:
            _normalize_crawl_config({"start_page": value})
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError(f"invalid start_page accepted: {value!r}")


def test_runner_records_risk_control_as_failed(monkeypatch, tmp_path):
    finishes = []
    popen_kwargs = []

    class FakeProcess:
        pid = 12345

        def wait(self):
            return RISK_CONTROL_EXIT_CODE

    monkeypatch.setattr(
        crawl_runner,
        "get_crawl_task",
        lambda _task_id: {
            "id": "crawl-risk",
            "status": "starting",
            "keywords": ["test"],
            "config": {"get_comments": True},
        },
    )
    monkeypatch.setattr(crawl_runner, "start_crawl_task", lambda *args: None)
    monkeypatch.setattr(crawl_runner, "set_crawl_worker_pid", lambda *args: None)
    account = {
        "account_id": "test",
        "user_data_dir": "%s_user_data_dir_test",
        "browser_path": str(tmp_path / "isolated-chromium"),
        "sqlite_db_path": str(tmp_path / "content.db"),
    }
    monkeypatch.setattr(
        crawl_runner,
        "bind_task_config",
        lambda *args, **kwargs: ({**account, "get_comments": True}, account),
    )
    monkeypatch.setattr(
        crawl_runner,
        "finish_crawl_task",
        lambda *args: finishes.append(args),
    )
    def fake_popen(*args, **kwargs):
        popen_kwargs.append(kwargs)
        return FakeProcess()

    monkeypatch.setattr(crawl_runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(crawl_runner, "record_completion", lambda **kwargs: None)
    monkeypatch.setattr(crawl_runner, "assert_launch_reserved", lambda *args: None)
    monkeypatch.setattr(crawl_runner, "confirm_launch", lambda *args: None)
    monkeypatch.setattr(crawl_runner, "acquire_profile_lock", lambda **kwargs: nullcontext())
    monkeypatch.setattr(crawl_runner, "launch_lease_heartbeat", lambda *args: nullcontext())
    monkeypatch.setattr(crawl_runner, "LOG_DIR", tmp_path)

    assert crawl_runner.run_task("crawl-risk") == RISK_CONTROL_EXIT_CODE
    assert finishes == [
        (
            "crawl-risk",
            RISK_CONTROL_EXIT_CODE,
            "XHS risk control CAPTCHA (HTTP 461/471); task stopped immediately",
        )
    ]
    assert "start_new_session" not in popen_kwargs[0]
