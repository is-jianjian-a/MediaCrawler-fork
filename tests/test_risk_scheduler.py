"""Queue behavior for the unattended Dashboard risk scheduler."""

from unittest import mock

from dashboard import comment_fetcher as _comment_fetcher  # Adds dashboard direct-script path.
from dashboard import server


def test_scheduler_prioritizes_oldest_auto_search():
    crawl_tasks = [
        {"id": "newer", "status": "pending", "config": {"start_mode": "auto"}},
        {"id": "older", "status": "pending", "config": {"start_mode": "auto"}},
    ]
    comment_tasks = [
        {"id": "comment", "status": "pending", "config_json": '{"start_mode":"auto"}'},
    ]
    with (
        mock.patch("crawl_task_manager.list_crawl_tasks", return_value=crawl_tasks),
        mock.patch("task_manager.list_tasks", return_value=comment_tasks),
        mock.patch.object(server, "_launch_crawl_task", return_value=({}, 202)) as launch_search,
        mock.patch.object(server, "_launch_comment_task") as launch_comment,
    ):
        assert server._auto_start_once() is True

    launch_search.assert_called_once_with("older")
    launch_comment.assert_not_called()


def test_scheduler_does_not_skip_denied_search_to_start_comments():
    crawl_tasks = [
        {"id": "canary", "status": "pending", "config": {"start_mode": "auto"}},
    ]
    comment_tasks = [
        {"id": "comment", "status": "pending", "config_json": '{"start_mode":"auto"}'},
    ]
    with (
        mock.patch("crawl_task_manager.list_crawl_tasks", return_value=crawl_tasks),
        mock.patch("task_manager.list_tasks", return_value=comment_tasks),
        mock.patch.object(server, "_launch_crawl_task", return_value=({}, 429)),
        mock.patch.object(server, "_launch_comment_task") as launch_comment,
    ):
        assert server._auto_start_once() is False

    launch_comment.assert_not_called()


def test_scheduler_stays_idle_when_only_pending_work_uses_active_account():
    crawl_tasks = [
        {
            "id": "active",
            "account_id": "A",
            "status": "running",
            "config": {"start_mode": "auto", "account_id": "A"},
        },
        {
            "id": "same-account",
            "account_id": "A",
            "status": "pending",
            "config": {"start_mode": "auto", "account_id": "A"},
        },
    ]
    with (
        mock.patch("crawl_task_manager.list_crawl_tasks", return_value=crawl_tasks),
        mock.patch("task_manager.list_tasks", return_value=[]),
        mock.patch.object(server, "_launch_crawl_task") as launch_search,
    ):
        assert server._auto_start_once() is False

    launch_search.assert_not_called()


def test_scheduler_starts_other_account_while_one_is_active():
    crawl_tasks = [
        {
            "id": "active-a",
            "account_id": "A",
            "status": "running",
            "config": {"start_mode": "auto", "account_id": "A"},
        },
        {
            "id": "pending-b",
            "account_id": "B",
            "status": "pending",
            "config": {"start_mode": "auto", "account_id": "B"},
        },
    ]
    with (
        mock.patch("crawl_task_manager.list_crawl_tasks", return_value=crawl_tasks),
        mock.patch("task_manager.list_tasks", return_value=[]),
        mock.patch.object(server, "_launch_crawl_task", return_value=({}, 202)) as launch_search,
    ):
        assert server._auto_start_once() is True

    launch_search.assert_called_once_with("pending-b")


def test_denied_account_does_not_block_another_account():
    crawl_tasks = [
        {
            "id": "pending-b",
            "account_id": "B",
            "status": "pending",
            "config": {"start_mode": "auto", "account_id": "B"},
        },
        {
            "id": "denied-a",
            "account_id": "A",
            "status": "pending",
            "config": {"start_mode": "auto", "account_id": "A"},
        },
    ]

    def launch(task_id):
        return ({}, 429 if task_id == "denied-a" else 202)

    with (
        mock.patch("crawl_task_manager.list_crawl_tasks", return_value=crawl_tasks),
        mock.patch("task_manager.list_tasks", return_value=[]),
        mock.patch.object(server, "_launch_crawl_task", side_effect=launch) as launch_search,
    ):
        assert server._auto_start_once() is True

    assert [call.args[0] for call in launch_search.call_args_list] == [
        "denied-a",
        "pending-b",
    ]
