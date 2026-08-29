"""Comment-task account routing and terminal-state tests."""

from dashboard import task_manager as manager


def _use_temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "task_manager.db"
    monkeypatch.setattr(manager, "TASK_DB", str(db_path))
    manager.init_task_db()
    return db_path


def test_comment_task_persists_account_id(monkeypatch, tmp_path):
    _use_temp_db(monkeypatch, tmp_path)
    task_id = manager.create_task(
        "comments-a",
        [{"note_id": "note-a"}],
        {"account_id": "A"},
        account_id="A",
    )
    assert manager.get_task(task_id)["account_id"] == "A"
    assert [task["id"] for task in manager.list_tasks(account_id="A")] == [task_id]
    assert manager.list_tasks(account_id="B") == []


def test_cancelled_comment_task_keeps_explicit_terminal_state(monkeypatch, tmp_path):
    _use_temp_db(monkeypatch, tmp_path)
    task_id = manager.create_task(
        "comments-a",
        [{"note_id": "note-a"}],
        {"account_id": "A"},
        account_id="A",
    )
    claimed, error = manager.claim_task(task_id)
    assert claimed, error
    manager.start_task(task_id, "/tmp/comment-a.log")
    manager.mark_task_cancelled(task_id, "通过 Dashboard 请求停止")
    manager.finish_task(task_id, 130)

    task = manager.get_task(task_id)
    assert task["status"] == "cancelled"
    assert task["exit_code"] == 130
    assert task["stop_source"] == "dashboard_api"
    assert task["stop_reason"] == "通过 Dashboard 请求停止"
