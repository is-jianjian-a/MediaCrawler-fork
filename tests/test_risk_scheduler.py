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


def test_reconciler_recovers_dead_workers_and_releases_both_task_types():
    search = {
        "id": "dead-search",
        "status": "running",
        "worker_pid": 12001,
        "started_at": 100.0,
        "account_id": "A",
        "config": {"account_id": "A"},
    }
    comment = {
        "id": "dead-comment",
        "status": "running",
        "worker_pid": 12002,
        "started_at": 100.0,
        "account_id": "B",
        "config_json": '{"account_id":"B"}',
    }
    with (
        mock.patch("crawl_task_manager.list_crawl_tasks", return_value=[search]),
        mock.patch("task_manager.list_tasks", return_value=[comment]),
        mock.patch("crawl_task_manager.get_crawl_task", return_value=search),
        mock.patch("task_manager.get_task", return_value=comment),
        mock.patch("crawl_task_manager.finish_crawl_task") as finish_search,
        mock.patch("task_manager.recover_lost_task") as recover_comment,
        mock.patch.object(server, "_pid_alive", return_value=False),
        mock.patch.object(server, "_terminate_task_group", return_value=True) as terminate,
        mock.patch.object(server, "_record_task_failure") as record_failure,
    ):
        assert server._reconcile_dead_workers(now=500.0) == 2

    assert terminate.call_count == 2
    finish_search.assert_called_once_with(
        "dead-search", 1, "worker process disappeared"
    )
    recover_comment.assert_called_once_with(
        "dead-comment", "worker process disappeared; unfinished posts reset"
    )
    assert record_failure.call_count == 2


def test_comment_cancel_does_not_mark_task_cancelled_when_group_survives():
    task = {
        "id": "stubborn-comment",
        "status": "running",
        "worker_pid": 12003,
    }
    with (
        mock.patch("task_manager.get_task", return_value=task),
        mock.patch("task_manager.mark_task_cancelled") as mark_cancelled,
        mock.patch.object(server, "_terminate_task_group", return_value=False),
        server.app.test_client() as client,
    ):
        response = client.post("/api/tasks/stubborn-comment/cancel")

    assert response.status_code == 500
    assert response.get_json()["killed"] is False
    mark_cancelled.assert_not_called()


def test_search_cancel_restores_running_state_when_group_survives():
    task = {
        "id": "stubborn-search",
        "status": "running",
        "worker_pid": 12004,
    }
    with (
        mock.patch("crawl_task_manager.get_crawl_task", return_value=task),
        mock.patch("crawl_task_manager.request_crawl_task_stop", return_value=(True, "")),
        mock.patch("crawl_task_manager.clear_crawl_task_stop_request") as clear_stop,
        mock.patch("crawl_task_manager.mark_crawl_task_cancelled") as mark_cancelled,
        mock.patch.object(server, "_terminate_task_group", return_value=False),
        server.app.test_client() as client,
    ):
        response = client.post("/api/crawl-tasks/stubborn-search/cancel")

    assert response.status_code == 500
    assert response.get_json()["killed"] is False
    clear_stop.assert_called_once()
    mark_cancelled.assert_not_called()


def test_pid_alive_reaps_finished_dashboard_child():
    with (
        mock.patch.object(server.os, "waitpid", return_value=(12005, 0)),
        mock.patch.object(server.os, "kill") as kill,
    ):
        assert server._pid_alive(12005) is False
    kill.assert_not_called()


def test_process_group_alive_detects_child_after_wrapper_exit():
    with (
        mock.patch.object(server, "_pid_alive", return_value=False),
        mock.patch.object(server.os, "killpg", return_value=None),
    ):
        assert server._process_group_alive(12006) is True
