"""Crawl-task terminal states must preserve completion, stop and failure semantics."""
import sqlite3

from dashboard import crawl_task_manager as manager


def _use_temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "task_manager.db"
    monkeypatch.setattr(manager, "TASK_DB", str(db_path))
    manager.init_crawl_task_db()
    return db_path


def _running_task(name="test"):
    task_id = manager.create_crawl_task(name, ["关键词"], {})
    ok, error = manager.claim_crawl_task(task_id)
    assert ok, error
    manager.start_crawl_task(task_id, "/tmp/test.log", 12345)
    return task_id


def test_dashboard_stop_is_not_overwritten_by_exit_130(monkeypatch, tmp_path):
    _use_temp_db(monkeypatch, tmp_path)
    task_id = _running_task()

    ok, error = manager.request_crawl_task_stop(task_id)
    assert ok, error
    assert manager.get_crawl_task(task_id)["status"] == "stopping"

    manager.finish_crawl_task(task_id, 130, "crawler exited with code 130")
    task = manager.get_crawl_task(task_id)
    assert task["status"] == "cancelled"
    assert task["exit_code"] == 130
    assert task["stop_source"] == "dashboard_api"
    assert task["error_message"] == "通过 Dashboard 请求停止"


def test_success_and_runtime_failure_remain_distinct(monkeypatch, tmp_path):
    _use_temp_db(monkeypatch, tmp_path)
    completed_id = _running_task("completed")
    failed_id = _running_task("failed")

    manager.finish_crawl_task(completed_id, 0)
    manager.finish_crawl_task(failed_id, 75, "XHS risk control")

    assert manager.get_crawl_task(completed_id)["status"] == "completed"
    failed = manager.get_crawl_task(failed_id)
    assert failed["status"] == "failed"
    assert failed["error_message"] == "XHS risk control"


def test_restarting_cancelled_task_clears_stop_metadata(monkeypatch, tmp_path):
    _use_temp_db(monkeypatch, tmp_path)
    task_id = _running_task()
    manager.request_crawl_task_stop(task_id)
    manager.finish_crawl_task(task_id, 130)

    ok, error = manager.claim_crawl_task(task_id)
    assert ok, error
    task = manager.get_crawl_task(task_id)
    assert task["status"] == "starting"
    assert task["stop_requested_at"] is None
    assert task["stop_source"] is None
    assert task["stop_reason"] is None


def test_old_exit_130_records_migrate_to_cancelled(monkeypatch, tmp_path):
    db_path = tmp_path / "task_manager.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE crawl_tasks (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT, created_at REAL,
            started_at REAL, completed_at REAL, keywords_json TEXT, config_json TEXT,
            log_path TEXT, worker_pid INTEGER, archived_at REAL,
            error_message TEXT, exit_code INTEGER
        )
        """
    )
    conn.execute(
        """
        INSERT INTO crawl_tasks
        (id, name, status, created_at, keywords_json, config_json, error_message, exit_code)
        VALUES ('crawl-old', 'old', 'failed', 1, '[]', '{}', 'crawler exited with code 130', 130)
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(manager, "TASK_DB", str(db_path))

    manager.init_crawl_task_db()
    task = manager.get_crawl_task("crawl-old")
    assert task["status"] == "cancelled"
    assert task["stop_source"] == "legacy_unknown"


def test_crawl_tasks_are_queryable_by_account(monkeypatch, tmp_path):
    _use_temp_db(monkeypatch, tmp_path)
    task_a = manager.create_crawl_task(
        "account-a", ["词A"], {"account_id": "A"}, account_id="A"
    )
    task_b = manager.create_crawl_task(
        "account-b", ["词B"], {"account_id": "B"}, account_id="B"
    )

    assert [task["id"] for task in manager.list_crawl_tasks(account_id="A")] == [
        task_a
    ]
    assert [task["id"] for task in manager.list_crawl_tasks(account_id="B")] == [
        task_b
    ]
