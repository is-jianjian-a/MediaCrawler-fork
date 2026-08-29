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


def test_scheduler_stays_idle_while_any_worker_is_active():
    crawl_tasks = [
        {"id": "active", "status": "running", "config": {"start_mode": "auto"}},
    ]
    with (
        mock.patch("crawl_task_manager.list_crawl_tasks", return_value=crawl_tasks),
        mock.patch("task_manager.list_tasks", return_value=[]),
        mock.patch.object(server, "_launch_crawl_task") as launch_search,
    ):
        assert server._auto_start_once() is False

    launch_search.assert_not_called()
