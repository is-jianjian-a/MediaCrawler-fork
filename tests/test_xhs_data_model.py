"""Normalized XHS account/store/run catalog tests; no browser is started."""

import sqlite3
from pathlib import Path

import pytest

from dashboard import xhs_data_model as model


def _content_db(path: Path, notes=(), comments=(), hits=()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE xhs_note (
            id INTEGER PRIMARY KEY,
            note_id TEXT UNIQUE,
            title TEXT,
            last_modify_ts INTEGER,
            crawler_account TEXT
        );
        CREATE TABLE xhs_note_comment (
            id INTEGER PRIMARY KEY,
            comment_id TEXT UNIQUE,
            note_id TEXT,
            content TEXT,
            crawler_account TEXT
        );
        CREATE TABLE xhs_note_keyword_hit (
            id INTEGER PRIMARY KEY,
            note_id TEXT,
            keyword TEXT,
            task_id TEXT,
            hit_count INTEGER
        );
        """
    )
    conn.executemany(
        "INSERT INTO xhs_note(note_id,title,last_modify_ts,crawler_account) "
        "VALUES (?,?,?,?)",
        notes,
    )
    conn.executemany(
        "INSERT INTO xhs_note_comment(comment_id,note_id,content,crawler_account) "
        "VALUES (?,?,?,?)",
        comments,
    )
    conn.executemany(
        "INSERT INTO xhs_note_keyword_hit(note_id,keyword,task_id,hit_count) "
        "VALUES (?,?,?,?)",
        hits,
    )
    conn.commit()
    conn.close()


def _control_db(path: Path, working_db: Path, legacy_db: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE xhs_accounts (
            account_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            user_data_dir TEXT NOT NULL UNIQUE,
            browser_path TEXT NOT NULL,
            sqlite_db_path TEXT NOT NULL,
            storage_mode TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE crawl_tasks (
            id TEXT PRIMARY KEY,
            account_id TEXT,
            status TEXT,
            config_json TEXT,
            created_at REAL,
            started_at REAL,
            completed_at REAL,
            exit_code INTEGER,
            error_message TEXT,
            stop_reason TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO xhs_accounts VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "02",
            "小红书账号 02",
            "%s_user_data_dir_account02",
            "/isolated/chromium",
            str(working_db),
            "dedicated",
            1,
            1,
            1,
        ),
    )
    conn.execute(
        "INSERT INTO xhs_accounts VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "legacy-default",
            "历史账号 legacy-default",
            "%s_user_data_dir_accountlegacy-default",
            "",
            str(legacy_db),
            "legacy_shared",
            0,
            1,
            1,
        ),
    )
    conn.execute(
        "INSERT INTO crawl_tasks VALUES "
        "('crawl-a','02','starting','{}',1,2,NULL,NULL,NULL,NULL)"
    )
    conn.commit()
    conn.close()


def test_catalog_separates_identity_routes_and_deduplicates_sources(tmp_path):
    task_db = tmp_path / "dashboard" / "task_manager.db"
    working_db = tmp_path / "database" / "accounts" / "02" / "content.db"
    legacy_db = tmp_path / "database" / "sqlite_tables.db"
    archive_root = tmp_path / "database" / ".archive"
    archive_db = archive_root / "xhs_account_03.db"
    _content_db(
        working_db,
        notes=[("n-shared", "working", 20, "01")],
        comments=[("c-shared", "n-shared", "working", "02")],
        hits=[("n-shared", "地铁", "crawl-a", 2)],
    )
    _content_db(
        legacy_db,
        notes=[
            ("n-shared", "legacy", 10, "01"),
            ("n-legacy", "legacy-only", 10, "03"),
        ],
        comments=[("c-shared", "n-shared", "legacy", "01")],
        hits=[("n-shared", "地铁", "crawl-a", 1)],
    )
    _content_db(
        archive_db,
        notes=[("n-archive", "archive-only", 5, "03")],
        comments=[("c-archive", "n-archive", "archive", "03")],
    )
    _control_db(task_db, working_db, legacy_db)

    model.init_xhs_data_model_db(
        task_db, legacy_content_db=legacy_db, archive_root=archive_root
    )

    conn = sqlite3.connect(task_db)
    conn.row_factory = sqlite3.Row
    accounts = {
        row["account_id"]: dict(row)
        for row in conn.execute("SELECT * FROM xhs_accounts")
    }
    stores = [dict(row) for row in conn.execute("SELECT * FROM xhs_data_stores")]
    conn.close()
    assert accounts["02"]["record_kind"] == "account"
    assert accounts["legacy-default"]["record_kind"] == "historical_placeholder"
    assert accounts["02"]["active_profile_id"]
    assert accounts["02"]["active_store_id"]
    assert {store["store_kind"] for store in stores} == {
        "account_working",
        "legacy_aggregate",
        "archive",
    }

    catalog = model.open_content_catalog(task_db=task_db)
    try:
        assert catalog.execute("SELECT COUNT(*) FROM xhs_note").fetchone()[0] == 3
        assert catalog.execute(
            "SELECT title FROM xhs_note WHERE note_id='n-shared'"
        ).fetchone()[0] == "working"
        assert catalog.execute(
            "SELECT COUNT(*) FROM xhs_note_comment"
        ).fetchone()[0] == 2
        assert catalog.execute(
            "SELECT COUNT(*) FROM xhs_note_keyword_hit"
        ).fetchone()[0] == 1
    finally:
        catalog.close()


def test_each_retry_gets_a_distinct_run_bound_to_profile_and_store(tmp_path):
    task_db = tmp_path / "dashboard" / "task_manager.db"
    working_db = tmp_path / "database" / "accounts" / "02" / "content.db"
    legacy_db = tmp_path / "database" / "sqlite_tables.db"
    _content_db(working_db)
    _content_db(legacy_db)
    _control_db(task_db, working_db, legacy_db)
    model.init_xhs_data_model_db(
        task_db,
        legacy_content_db=legacy_db,
        archive_root=tmp_path / "missing-archives",
    )
    account = {"account_id": "02"}

    first = model.begin_task_run(
        task_id="crawl-a",
        task_kind="search",
        account=account,
        config={"keywords": ["地铁"]},
        task_db=task_db,
    )
    model.mark_task_run_running(first["run_id"], worker_pid=123, task_db=task_db)
    model.finish_task_run(
        first["run_id"], exit_code=75, status="failed", task_db=task_db
    )
    second = model.begin_task_run(
        task_id="crawl-a",
        task_kind="search",
        account=account,
        config={"keywords": ["地铁"]},
        task_db=task_db,
    )

    assert first["run_id"] != second["run_id"]
    assert first["attempt"] == 1
    assert second["attempt"] == 2
    assert first["profile_id"] == second["profile_id"]
    assert first["store_id"] == second["store_id"]
    current = model.current_task_run("crawl-a", "search", task_db=task_db)
    assert current["run_id"] == second["run_id"]
    assert current["status"] == "starting"


def test_dashboard_run_rejects_mismatched_route_identity(tmp_path):
    task_db = tmp_path / "dashboard" / "task_manager.db"
    working_db = tmp_path / "database" / "accounts" / "02" / "content.db"
    legacy_db = tmp_path / "database" / "sqlite_tables.db"
    _content_db(working_db)
    _content_db(legacy_db)
    _control_db(task_db, working_db, legacy_db)
    model.init_xhs_data_model_db(
        task_db,
        legacy_content_db=legacy_db,
        archive_root=tmp_path / "missing-archives",
    )

    conn = sqlite3.connect(task_db)
    route = conn.execute(
        "SELECT profile_id,store_id,route_id FROM xhs_store_routes "
        "WHERE account_id='02' AND route_role='primary'"
    ).fetchone()
    with pytest.raises(sqlite3.IntegrityError, match="route is not writable"):
        conn.execute(
            "INSERT INTO xhs_runs "
            "(run_id,task_id,task_kind,attempt,account_id,profile_id,store_id,"
            "route_id,status,config_json,created_at) "
            "VALUES ('bad','crawl-a','search',1,'wrong',?,?,?,'starting','{}',1)",
            route,
        )
    conn.close()


def test_legacy_seed_marker_does_not_relabel_content_rows(tmp_path):
    task_db = tmp_path / "dashboard" / "task_manager.db"
    working_db = tmp_path / "database" / "accounts" / "02" / "content.db"
    legacy_db = tmp_path / "database" / "sqlite_tables.db"
    _content_db(working_db, notes=[("n1", "kept", 1, "01")])
    _content_db(legacy_db)
    _control_db(task_db, working_db, legacy_db)
    model.init_xhs_data_model_db(
        task_db,
        legacy_content_db=legacy_db,
        archive_root=tmp_path / "missing-archives",
    )

    metadata = model.mark_account_store_legacy_seeded(
        "02", seed_cutoff=1234, task_db=task_db
    )

    assert metadata["store_kind"] == "legacy_seeded_working"
    assert metadata["seed_cutoff"] == 1234
    content = sqlite3.connect(working_db)
    try:
        assert content.execute(
            "SELECT crawler_account FROM xhs_note WHERE note_id='n1'"
        ).fetchone()[0] == "01"
    finally:
        content.close()


def test_legacy_task_import_keeps_placeholder_unattributed(tmp_path):
    task_db = tmp_path / "dashboard" / "task_manager.db"
    working_db = tmp_path / "database" / "accounts" / "02" / "content.db"
    legacy_db = tmp_path / "database" / "sqlite_tables.db"
    _content_db(working_db)
    _content_db(legacy_db)
    _control_db(task_db, working_db, legacy_db)
    conn = sqlite3.connect(task_db)
    conn.execute(
        "INSERT INTO crawl_tasks VALUES "
        "('crawl-legacy','legacy-default','completed','{}',1,2,3,0,NULL,NULL)"
    )
    conn.commit()
    conn.close()
    model.init_xhs_data_model_db(
        task_db,
        legacy_content_db=legacy_db,
        archive_root=tmp_path / "missing-archives",
    )

    result = model.import_legacy_task_runs(task_db=task_db)

    assert result["search"] == 2
    conn = sqlite3.connect(task_db)
    conn.row_factory = sqlite3.Row
    try:
        placeholder = conn.execute(
            "SELECT * FROM xhs_runs WHERE task_id='crawl-legacy'"
        ).fetchone()
        account_run = conn.execute(
            "SELECT * FROM xhs_runs WHERE task_id='crawl-a'"
        ).fetchone()
        assert placeholder["account_id"] is None
        assert placeholder["profile_id"] is None
        assert placeholder["attribution_status"] == "legacy_unattributed"
        assert placeholder["status"] == "completed"
        assert account_run["account_id"] == "02"
        assert account_run["status"] == "aborted"
        assert account_run["run_origin"] == "legacy_task_snapshot"
    finally:
        conn.close()
