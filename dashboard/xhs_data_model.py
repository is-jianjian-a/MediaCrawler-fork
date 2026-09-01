#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Normalized XHS identity, route, store and run metadata.

The legacy ``xhs_accounts`` table remains the compatibility route used by
workers.  These tables separate that route into auditable concepts without
rewriting historical content databases.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from config.runtime_paths import (
    BROWSER_DATA_ROOT,
    CONTENT_ARCHIVE_ROOT as ARCHIVE_ROOT,
    LEGACY_CONTENT_DB,
    TASK_DB,
)


MEDIACRAWLER_ROOT = Path(__file__).resolve().parents[1]


class XhsDataModelError(ValueError):
    """Raised when normalized task provenance cannot be recorded safely."""


def _connect(task_db: Path | str = TASK_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(task_db), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
    )


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def resolved_profile_path(
    user_data_dir: str, *, browser_data_root: Path | str = BROWSER_DATA_ROOT
) -> str:
    template = str(user_data_dir or "").strip()
    if template.count("%s") != 1:
        raise XhsDataModelError("user_data_dir must contain exactly one %s placeholder")
    relative = template % "xhs"
    return str((Path(browser_data_root) / relative).resolve(strict=False))


def profile_id_for_route(
    user_data_dir: str, *, browser_data_root: Path | str = BROWSER_DATA_ROOT
) -> str:
    return _stable_id(
        "profile",
        resolved_profile_path(user_data_dir, browser_data_root=browser_data_root),
    )


def store_id_for_path(sqlite_db_path: str) -> str:
    path = str(Path(sqlite_db_path).expanduser().resolve(strict=False))
    return _stable_id("store", path)


def _add_compatibility_columns(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "xhs_accounts"):
        return
    existing = _columns(conn, "xhs_accounts")
    additions = {
        "record_kind": "ALTER TABLE xhs_accounts ADD COLUMN record_kind TEXT NOT NULL DEFAULT 'account'",
        "identity_status": "ALTER TABLE xhs_accounts ADD COLUMN identity_status TEXT NOT NULL DEFAULT 'unverified'",
        "active_profile_id": "ALTER TABLE xhs_accounts ADD COLUMN active_profile_id TEXT",
        "active_store_id": "ALTER TABLE xhs_accounts ADD COLUMN active_store_id TEXT",
        "active_route_id": "ALTER TABLE xhs_accounts ADD COLUMN active_route_id TEXT",
    }
    for column, ddl in additions.items():
        if column not in existing:
            conn.execute(ddl)
    conn.execute(
        "UPDATE xhs_accounts SET record_kind='historical_placeholder', "
        "identity_status='unknown' WHERE account_id LIKE 'legacy-%'"
    )

    for table_name in ("tasks", "crawl_tasks"):
        if not _table_exists(conn, table_name):
            continue
        if "current_run_id" not in _columns(conn, table_name):
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN current_run_id TEXT")


def _create_normalized_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS xhs_browser_profiles (
            profile_id TEXT PRIMARY KEY,
            account_id TEXT,
            user_data_dir TEXT NOT NULL UNIQUE,
            resolved_path TEXT NOT NULL DEFAULT '',
            browser_path TEXT NOT NULL DEFAULT '',
            profile_status TEXT NOT NULL DEFAULT 'unverified',
            last_verified_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            CHECK (profile_status IN ('active','unverified','retired'))
        );

        CREATE INDEX IF NOT EXISTS idx_xhs_browser_profiles_account_status
        ON xhs_browser_profiles(account_id, profile_status);

        CREATE TABLE IF NOT EXISTS xhs_data_stores (
            store_id TEXT PRIMARY KEY,
            account_id TEXT,
            display_name TEXT NOT NULL,
            store_kind TEXT NOT NULL,
            sqlite_db_path TEXT NOT NULL UNIQUE,
            read_only INTEGER NOT NULL DEFAULT 0,
            catalog_enabled INTEGER NOT NULL DEFAULT 1,
            priority INTEGER NOT NULL DEFAULT 100,
            seed_store_id TEXT,
            seed_cutoff REAL,
            legacy_source_label TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            CHECK (store_kind IN (
                'account_working','legacy_seeded_working',
                'legacy_aggregate','archive'
            )),
            CHECK (read_only IN (0,1)),
            CHECK (catalog_enabled IN (0,1)),
            FOREIGN KEY (seed_store_id) REFERENCES xhs_data_stores(store_id)
        );

        CREATE INDEX IF NOT EXISTS idx_xhs_data_stores_account_kind
        ON xhs_data_stores(account_id, store_kind);

        CREATE TABLE IF NOT EXISTS xhs_store_routes (
            route_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            store_id TEXT NOT NULL,
            route_role TEXT NOT NULL DEFAULT 'primary',
            access_mode TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE (account_id, profile_id, store_id),
            CHECK (route_role IN ('primary','compatibility','historical')),
            CHECK (access_mode IN ('read_only','read_write')),
            CHECK (active IN (0,1)),
            FOREIGN KEY (profile_id) REFERENCES xhs_browser_profiles(profile_id),
            FOREIGN KEY (store_id) REFERENCES xhs_data_stores(store_id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS ux_xhs_store_routes_primary
        ON xhs_store_routes(account_id)
        WHERE active=1 AND route_role='primary';

        CREATE TABLE IF NOT EXISTS xhs_runs (
            run_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            task_kind TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            account_id TEXT,
            profile_id TEXT,
            store_id TEXT,
            route_id TEXT,
            status TEXT NOT NULL DEFAULT 'starting',
            run_origin TEXT NOT NULL DEFAULT 'dashboard',
            attribution_status TEXT NOT NULL DEFAULT 'direct',
            config_json TEXT NOT NULL DEFAULT '{}',
            worker_pid INTEGER,
            created_at REAL NOT NULL,
            started_at REAL,
            completed_at REAL,
            exit_code INTEGER,
            stop_reason TEXT,
            legacy_table TEXT,
            legacy_task_id TEXT,
            UNIQUE (task_id, task_kind, attempt),
            CHECK (task_kind IN ('search','comment','import')),
            CHECK (status IN (
                'starting','running','completed','completed_with_errors',
                'failed','cancelled','aborted'
            )),
            FOREIGN KEY (profile_id) REFERENCES xhs_browser_profiles(profile_id),
            FOREIGN KEY (store_id) REFERENCES xhs_data_stores(store_id),
            FOREIGN KEY (route_id) REFERENCES xhs_store_routes(route_id)
        );

        CREATE INDEX IF NOT EXISTS idx_xhs_runs_task
        ON xhs_runs(task_kind, task_id, attempt);
        CREATE INDEX IF NOT EXISTS idx_xhs_runs_account_status
        ON xhs_runs(account_id, status);

        CREATE TRIGGER IF NOT EXISTS trg_xhs_runs_validate_dashboard_route
        BEFORE INSERT ON xhs_runs
        WHEN NEW.run_origin='dashboard'
        BEGIN
            SELECT CASE WHEN NEW.account_id IS NULL
                              OR NEW.profile_id IS NULL
                              OR NEW.store_id IS NULL
                              OR NEW.route_id IS NULL
                         THEN RAISE(ABORT, 'dashboard run requires a complete route')
                   END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM xhs_store_routes r
                JOIN xhs_data_stores s ON s.store_id=r.store_id
                WHERE r.route_id=NEW.route_id
                  AND r.account_id=NEW.account_id
                  AND r.profile_id=NEW.profile_id
                  AND r.store_id=NEW.store_id
                  AND r.active=1
                  AND r.access_mode='read_write'
                  AND s.read_only=0
            ) THEN RAISE(ABORT, 'dashboard run route is not writable') END;
        END;

        CREATE TABLE IF NOT EXISTS xhs_schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            applied_at REAL NOT NULL
        );
        """
    )
    profile_columns = _columns(conn, "xhs_browser_profiles")
    if "resolved_path" not in profile_columns:
        conn.execute(
            "ALTER TABLE xhs_browser_profiles ADD COLUMN resolved_path TEXT NOT NULL DEFAULT ''"
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_xhs_browser_profiles_resolved_path "
        "ON xhs_browser_profiles(resolved_path COLLATE NOCASE) "
        "WHERE resolved_path <> ''"
    )
    run_columns = _columns(conn, "xhs_runs")
    for column, ddl in {
        "route_id": "ALTER TABLE xhs_runs ADD COLUMN route_id TEXT",
        "run_origin": "ALTER TABLE xhs_runs ADD COLUMN run_origin TEXT NOT NULL DEFAULT 'dashboard'",
        "attribution_status": "ALTER TABLE xhs_runs ADD COLUMN attribution_status TEXT NOT NULL DEFAULT 'direct'",
        "legacy_table": "ALTER TABLE xhs_runs ADD COLUMN legacy_table TEXT",
        "legacy_task_id": "ALTER TABLE xhs_runs ADD COLUMN legacy_task_id TEXT",
    }.items():
        if column not in run_columns:
            conn.execute(ddl)
    conn.execute(
        "INSERT OR IGNORE INTO xhs_schema_migrations(version,name,applied_at) "
        "VALUES (1,'normalized-account-store-run-catalog',?)",
        (time.time(),),
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_xhs_runs_legacy_task "
        "ON xhs_runs(legacy_table,legacy_task_id) "
        "WHERE legacy_table IS NOT NULL AND legacy_task_id IS NOT NULL"
    )


def _upsert_store(
    conn: sqlite3.Connection,
    *,
    sqlite_db_path: str,
    display_name: str,
    store_kind: str,
    account_id: Optional[str] = None,
    read_only: bool,
    priority: int,
    legacy_source_label: Optional[str] = None,
) -> str:
    path = str(Path(sqlite_db_path).expanduser().resolve(strict=False))
    now = time.time()
    existing = conn.execute(
        "SELECT store_id FROM xhs_data_stores WHERE sqlite_db_path=?",
        (path,),
    ).fetchone()
    store_id = str(existing[0]) if existing else store_id_for_path(path)
    conn.execute(
        """
        INSERT INTO xhs_data_stores
        (store_id, account_id, display_name, store_kind, sqlite_db_path,
         read_only, catalog_enabled, priority, legacy_source_label,
         created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
        ON CONFLICT(store_id) DO UPDATE SET
            account_id=excluded.account_id,
            display_name=excluded.display_name,
            sqlite_db_path=excluded.sqlite_db_path,
            updated_at=excluded.updated_at
        """,
        (
            store_id,
            account_id,
            display_name,
            store_kind,
            path,
            1 if read_only else 0,
            int(priority),
            legacy_source_label,
            now,
            now,
        ),
    )
    return store_id


def _sync_account_routes(
    conn: sqlite3.Connection,
    *,
    legacy_content_db: Path,
    browser_data_root: Path,
) -> None:
    if not _table_exists(conn, "xhs_accounts"):
        return
    legacy_path = str(legacy_content_db.resolve(strict=False))
    for row in conn.execute("SELECT * FROM xhs_accounts").fetchall():
        account = dict(row)
        account_id = str(account["account_id"])
        record_kind = str(account.get("record_kind") or "account")
        resolved_path = resolved_profile_path(
            account["user_data_dir"], browser_data_root=browser_data_root
        )
        generated_profile_id = profile_id_for_route(
            account["user_data_dir"], browser_data_root=browser_data_root
        )
        existing_profile = conn.execute(
            "SELECT profile_id FROM xhs_browser_profiles WHERE user_data_dir=?",
            (account["user_data_dir"],),
        ).fetchone()
        profile_id = (
            str(existing_profile[0]) if existing_profile else generated_profile_id
        )
        profile_status = (
            "unverified"
            if record_kind == "historical_placeholder"
            else ("active" if account.get("enabled") else "unverified")
        )
        now = time.time()
        conn.execute(
            """
            INSERT INTO xhs_browser_profiles
            (profile_id, account_id, user_data_dir, resolved_path, browser_path, profile_status,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_id) DO UPDATE SET
                account_id=excluded.account_id,
                resolved_path=excluded.resolved_path,
                browser_path=excluded.browser_path,
                profile_status=excluded.profile_status,
                updated_at=excluded.updated_at
            """,
            (
                profile_id,
                None if record_kind == "historical_placeholder" else account_id,
                account["user_data_dir"],
                resolved_path,
                account.get("browser_path") or "",
                profile_status,
                now,
                now,
            ),
        )

        db_path = str(Path(account["sqlite_db_path"]).resolve(strict=False))
        is_legacy = (
            db_path == legacy_path or account.get("storage_mode") == "legacy_shared"
        )
        store_id = _upsert_store(
            conn,
            sqlite_db_path=db_path,
            display_name=(
                "历史聚合主库"
                if is_legacy
                else f"{account.get('display_name') or account_id} 工作库"
            ),
            store_kind="legacy_aggregate" if is_legacy else "account_working",
            account_id=None if is_legacy else account_id,
            read_only=is_legacy,
            priority=10 if is_legacy else 100,
            legacy_source_label="aggregate" if is_legacy else None,
        )
        route_id = _stable_id(
            "route", f"{account_id}:{profile_id}:{store_id}"
        )
        route_role = (
            "historical"
            if record_kind == "historical_placeholder"
            else "primary"
        )
        access_mode = "read_only" if is_legacy else "read_write"
        if route_role == "primary":
            conn.execute(
                "UPDATE xhs_store_routes SET active=0, route_role='compatibility', "
                "updated_at=? WHERE account_id=? AND route_id<>? "
                "AND active=1 AND route_role='primary'",
                (now, account_id, route_id),
            )
        conn.execute(
            """
            INSERT INTO xhs_store_routes
            (route_id, account_id, profile_id, store_id, route_role,
             access_mode, active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(route_id) DO UPDATE SET
                route_role=excluded.route_role,
                access_mode=excluded.access_mode,
                active=excluded.active,
                updated_at=excluded.updated_at
            """,
            (
                route_id,
                account_id,
                profile_id,
                store_id,
                route_role,
                access_mode,
                now,
                now,
            ),
        )
        conn.execute(
            "UPDATE xhs_accounts SET active_profile_id=?, active_store_id=?, "
            "active_route_id=? "
            "WHERE account_id=?",
            (profile_id, store_id, route_id, account_id),
        )


def _archive_label(path: Path) -> str:
    name = path.name
    if name.startswith("xhs_account_") and name.endswith(".db"):
        return name[len("xhs_account_") : -len(".db")]
    return ""


def _register_archives(conn: sqlite3.Connection, archive_root: Path) -> None:
    if not archive_root.exists():
        return
    for path in sorted(archive_root.glob("xhs_account_*.db")):
        label = _archive_label(path)
        _upsert_store(
            conn,
            sqlite_db_path=str(path),
            display_name=f"历史账号 {label or path.stem} 原始归档",
            store_kind="archive",
            read_only=True,
            priority=5,
            legacy_source_label=label or None,
        )


def init_xhs_data_model_db(
    task_db: Path | str = TASK_DB,
    *,
    legacy_content_db: Path | str = LEGACY_CONTENT_DB,
    archive_root: Path | str = ARCHIVE_ROOT,
    browser_data_root: Path | str = BROWSER_DATA_ROOT,
) -> None:
    """Create and backfill normalized metadata without touching content rows."""
    task_db = Path(task_db)
    task_db.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(task_db)
    try:
        _create_normalized_tables(conn)
        _add_compatibility_columns(conn)
        _sync_account_routes(
            conn,
            legacy_content_db=Path(legacy_content_db),
            browser_data_root=Path(browser_data_root),
        )
        _register_archives(conn, Path(archive_root))
        conn.commit()
    finally:
        conn.close()


def route_metadata(
    account_id: str,
    *,
    task_db: Path | str = TASK_DB,
    require_writable: bool = False,
) -> Dict[str, Any]:
    conn = _connect(task_db)
    try:
        row = conn.execute(
            """
            SELECT a.account_id, a.record_kind, a.identity_status,
                   a.active_profile_id AS profile_id,
                   a.active_store_id AS store_id,
                   a.active_route_id AS route_id,
                   p.profile_status, p.resolved_path,
                   s.store_kind, s.read_only,
                   s.seed_store_id, s.seed_cutoff,
                   r.access_mode, r.active AS route_active
            FROM xhs_accounts a
            LEFT JOIN xhs_browser_profiles p
              ON p.profile_id = a.active_profile_id
            LEFT JOIN xhs_data_stores s
              ON s.store_id = a.active_store_id
            LEFT JOIN xhs_store_routes r
              ON r.route_id = a.active_route_id
             AND r.account_id = a.account_id
             AND r.profile_id = a.active_profile_id
             AND r.store_id = a.active_store_id
            WHERE a.account_id=?
            """,
            (account_id,),
        ).fetchone()
    finally:
        conn.close()
    if (
        not row
        or not row["profile_id"]
        or not row["store_id"]
        or not row["route_id"]
    ):
        raise XhsDataModelError(f"normalized route is missing for account {account_id}")
    result = dict(row)
    if require_writable and (
        result.get("record_kind") != "account"
        or bool(result.get("read_only"))
        or result.get("access_mode") != "read_write"
        or not bool(result.get("route_active"))
    ):
        raise XhsDataModelError(f"account {account_id} has no writable active route")
    return result


def begin_task_run(
    *,
    task_id: str,
    task_kind: str,
    account: Dict[str, Any],
    config: Dict[str, Any],
    task_db: Path | str = TASK_DB,
) -> Dict[str, Any]:
    """Create one immutable attempt record and bind it to the task."""
    if task_kind not in ("search", "comment"):
        raise XhsDataModelError(f"unsupported task kind: {task_kind}")
    task_table = "crawl_tasks" if task_kind == "search" else "tasks"
    account_id = str(account.get("account_id") or "").strip()
    if not account_id:
        raise XhsDataModelError("account_id is required for a Dashboard run")
    schema_conn = _connect(task_db)
    try:
        schema_ready = _table_exists(schema_conn, "xhs_runs")
    finally:
        schema_conn.close()
    if not schema_ready:
        init_xhs_data_model_db(task_db)
    metadata = route_metadata(account_id, task_db=task_db, require_writable=True)
    conn = _connect(task_db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        task_row = conn.execute(
            f"SELECT id FROM {task_table} WHERE id=?", (task_id,)
        ).fetchone()
        if not task_row:
            raise XhsDataModelError(f"task not found: {task_id}")
        attempt = int(
            conn.execute(
                "SELECT COALESCE(MAX(attempt), 0) + 1 FROM xhs_runs "
                "WHERE task_id=? AND task_kind=?",
                (task_id, task_kind),
            ).fetchone()[0]
        )
        run_id = f"run-{uuid.uuid4().hex[:16]}"
        now = time.time()
        conn.execute(
            """
            INSERT INTO xhs_runs
            (run_id, task_id, task_kind, attempt, account_id, profile_id,
             store_id, route_id, status, config_json, created_at, started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'starting', ?, ?, ?)
            """,
            (
                run_id,
                task_id,
                task_kind,
                attempt,
                account_id,
                metadata["profile_id"],
                metadata["store_id"],
                metadata["route_id"],
                json.dumps(config or {}, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        conn.execute(
            f"UPDATE {task_table} SET current_run_id=? WHERE id=?",
            (run_id, task_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "run_id": run_id,
        "attempt": attempt,
        "account_id": account_id,
        **metadata,
    }


def mark_task_run_running(
    run_id: str,
    *,
    worker_pid: Optional[int] = None,
    task_db: Path | str = TASK_DB,
) -> None:
    if not run_id:
        return
    conn = _connect(task_db)
    conn.execute(
        "UPDATE xhs_runs SET status='running', worker_pid=?, "
        "started_at=COALESCE(started_at, ?) WHERE run_id=? AND status='starting'",
        (worker_pid, time.time(), run_id),
    )
    conn.commit()
    conn.close()


def finish_task_run(
    run_id: str,
    *,
    exit_code: int,
    status: str = "",
    stop_reason: str = "",
    task_db: Path | str = TASK_DB,
) -> None:
    if not run_id:
        return
    final_status = status or ("completed" if exit_code == 0 else "failed")
    allowed = {
        "completed",
        "completed_with_errors",
        "failed",
        "cancelled",
        "aborted",
    }
    if final_status not in allowed:
        raise XhsDataModelError(f"invalid terminal run status: {final_status}")
    conn = _connect(task_db)
    conn.execute(
        "UPDATE xhs_runs SET status=?, completed_at=?, exit_code=?, "
        "stop_reason=?, worker_pid=NULL WHERE run_id=?",
        (final_status, time.time(), int(exit_code), stop_reason or None, run_id),
    )
    conn.commit()
    conn.close()


def current_task_run(
    task_id: str,
    task_kind: str,
    *,
    task_db: Path | str = TASK_DB,
) -> Optional[Dict[str, Any]]:
    table_name = "crawl_tasks" if task_kind == "search" else "tasks"
    conn = _connect(task_db)
    try:
        if not _table_exists(conn, table_name) or "current_run_id" not in _columns(
            conn, table_name
        ):
            return None
        row = conn.execute(
            f"""
            SELECT r.* FROM {table_name} t
            JOIN xhs_runs r ON r.run_id=t.current_run_id
            WHERE t.id=?
            """,
            (task_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def import_legacy_task_runs(
    *,
    task_db: Path | str = TASK_DB,
) -> Dict[str, int]:
    """Create non-authoritative run snapshots for tasks created before run IDs.

    Historical placeholders remain unattributed instead of being promoted to
    real accounts.  No content row or historical task configuration is changed.
    """
    conn = _connect(task_db)
    imported = {"search": 0, "comment": 0, "skipped_unstarted": 0}
    try:
        conn.execute("BEGIN IMMEDIATE")
        legacy_store = conn.execute(
            "SELECT store_id FROM xhs_data_stores "
            "WHERE store_kind='legacy_aggregate' ORDER BY priority DESC LIMIT 1"
        ).fetchone()
        for table_name, task_kind in (
            ("crawl_tasks", "search"),
            ("tasks", "comment"),
        ):
            if not _table_exists(conn, table_name):
                continue
            for row in conn.execute(f"SELECT * FROM {table_name}").fetchall():
                task = dict(row)
                started = task.get("started_at") is not None
                terminal = str(task.get("status") or "") in {
                    "completed",
                    "completed_with_errors",
                    "failed",
                    "cancelled",
                }
                if not started and not terminal and task.get("exit_code") is None:
                    imported["skipped_unstarted"] += 1
                    continue
                existing = conn.execute(
                    "SELECT 1 FROM xhs_runs WHERE legacy_table=? AND legacy_task_id=?",
                    (table_name, task["id"]),
                ).fetchone()
                if existing:
                    continue
                try:
                    config = json.loads(task.get("config_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    config = {}
                if not isinstance(config, dict):
                    config = {}

                stored_account_id = str(task.get("account_id") or "").strip()
                account_row = conn.execute(
                    "SELECT record_kind,active_profile_id FROM xhs_accounts "
                    "WHERE account_id=?",
                    (stored_account_id,),
                ).fetchone()
                real_account_id = (
                    stored_account_id
                    if account_row and account_row["record_kind"] == "account"
                    else None
                )
                profile_id = (
                    account_row["active_profile_id"] if real_account_id else None
                )

                configured_db = str(config.get("sqlite_db_path") or "").strip()
                store_row = None
                if configured_db:
                    configured_db = str(
                        Path(configured_db).expanduser().resolve(strict=False)
                    )
                    store_row = conn.execute(
                        "SELECT store_id FROM xhs_data_stores WHERE sqlite_db_path=?",
                        (configured_db,),
                    ).fetchone()
                store_id = (
                    store_row["store_id"]
                    if store_row
                    else (legacy_store["store_id"] if legacy_store else None)
                )
                route_row = None
                if real_account_id and profile_id and store_id:
                    route_row = conn.execute(
                        "SELECT route_id FROM xhs_store_routes "
                        "WHERE account_id=? AND profile_id=? AND store_id=? "
                        "ORDER BY active DESC LIMIT 1",
                        (real_account_id, profile_id, store_id),
                    ).fetchone()

                task_status = str(task.get("status") or "")
                status = (
                    task_status
                    if task_status
                    in {"completed", "completed_with_errors", "failed", "cancelled"}
                    else "aborted"
                )
                stop_reason = str(
                    task.get("stop_reason")
                    or task.get("error_message")
                    or (
                        "legacy task snapshot had no terminal state"
                        if status == "aborted"
                        else ""
                    )
                )
                run_id = _stable_id(
                    "run", f"legacy:{table_name}:{task['id']}"
                )
                conn.execute(
                    """
                    INSERT INTO xhs_runs
                    (run_id,task_id,task_kind,attempt,account_id,profile_id,
                     store_id,route_id,status,run_origin,attribution_status,
                     config_json,worker_pid,created_at,started_at,completed_at,
                     exit_code,stop_reason,legacy_table,legacy_task_id)
                    VALUES (?,?,?,?,?,?,?,?,?,'legacy_task_snapshot',
                            'legacy_unattributed',?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id,
                        task["id"],
                        task_kind,
                        1,
                        real_account_id,
                        profile_id,
                        store_id,
                        route_row["route_id"] if route_row else None,
                        status,
                        json.dumps(config, ensure_ascii=False, sort_keys=True),
                        None,
                        float(task.get("created_at") or 0),
                        task.get("started_at"),
                        task.get("completed_at"),
                        task.get("exit_code"),
                        stop_reason or None,
                        table_name,
                        task["id"],
                    ),
                )
                conn.execute(
                    f"UPDATE {table_name} SET current_run_id=COALESCE(current_run_id, ?) "
                    "WHERE id=?",
                    (run_id, task["id"]),
                )
                imported[task_kind] += 1
        conn.execute(
            "INSERT OR IGNORE INTO xhs_schema_migrations(version,name,applied_at) "
            "VALUES (2,'legacy-task-run-snapshots',?)",
            (time.time(),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return imported


def list_catalog_sources(
    *,
    task_db: Path | str = TASK_DB,
    existing_only: bool = True,
) -> list[Dict[str, Any]]:
    conn = _connect(task_db)
    rows = conn.execute(
        "SELECT * FROM xhs_data_stores WHERE catalog_enabled=1 "
        "ORDER BY priority DESC, created_at, store_id"
    ).fetchall()
    conn.close()
    result = [dict(row) for row in rows]
    if existing_only:
        result = [row for row in result if Path(row["sqlite_db_path"]).exists()]
    return result


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _attached_table_columns(
    conn: sqlite3.Connection, schema_name: str, table_name: str
) -> list[str]:
    rows = conn.execute(
        f"PRAGMA {_quote_identifier(schema_name)}.table_info({_quote_identifier(table_name)})"
    ).fetchall()
    return [str(row[1]) for row in rows]


def _create_catalog_view(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    key_columns: tuple[str, ...],
    attached_sources: list[tuple[str, Dict[str, Any]]],
) -> None:
    available: list[tuple[str, Dict[str, Any], list[str]]] = []
    for schema_name, source in attached_sources:
        columns = _attached_table_columns(conn, schema_name, table_name)
        if columns and all(key in columns for key in key_columns):
            available.append((schema_name, source, columns))
    if not available:
        return

    canonical_columns = available[0][2]
    selects: list[str] = []
    previous: list[tuple[str, list[str]]] = []
    quoted_table = _quote_identifier(table_name)
    for schema_name, _source, source_columns in available:
        source_set = set(source_columns)
        projection = ", ".join(
            (
                f"current.{_quote_identifier(column)} AS {_quote_identifier(column)}"
                if column in source_set
                else f"NULL AS {_quote_identifier(column)}"
            )
            for column in canonical_columns
        )
        exclusions = []
        for previous_schema, previous_columns in previous:
            if not all(key in previous_columns for key in key_columns):
                continue
            predicate = " AND ".join(
                f"prior.{_quote_identifier(key)} = current.{_quote_identifier(key)}"
                for key in key_columns
            )
            exclusions.append(
                "NOT EXISTS (SELECT 1 FROM "
                f"{_quote_identifier(previous_schema)}.{quoted_table} AS prior "
                f"WHERE {predicate})"
            )
        where_clause = f" WHERE {' AND '.join(exclusions)}" if exclusions else ""
        selects.append(
            f"SELECT {projection} FROM {_quote_identifier(schema_name)}."
            f"{quoted_table} AS current{where_clause}"
        )
        previous.append((schema_name, source_columns))
    conn.execute(
        f"CREATE TEMP VIEW {_quote_identifier(table_name)} AS "
        + " UNION ALL ".join(selects)
    )


def open_content_catalog(
    *,
    task_db: Path | str = TASK_DB,
) -> sqlite3.Connection:
    """Open a read-only, deduplicated view across registered content stores.

    Higher-priority working stores win over the legacy aggregate and archives.
    The source databases are attached read-only and are never rewritten.
    """
    sources = list_catalog_sources(task_db=task_db, existing_only=True)
    if not sources:
        raise XhsDataModelError("no readable XHS catalog sources are registered")
    conn = sqlite3.connect(":memory:", timeout=30, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    attached: list[tuple[str, Dict[str, Any]]] = []
    try:
        for index, source in enumerate(sources):
            schema_name = f"source_{index}"
            uri = Path(source["sqlite_db_path"]).resolve(strict=True).as_uri() + "?mode=ro"
            conn.execute(
                f"ATTACH DATABASE ? AS {_quote_identifier(schema_name)}", (uri,)
            )
            attached.append((schema_name, source))
        _create_catalog_view(
            conn,
            table_name="xhs_note",
            key_columns=("note_id",),
            attached_sources=attached,
        )
        _create_catalog_view(
            conn,
            table_name="xhs_note_comment",
            key_columns=("comment_id",),
            attached_sources=attached,
        )
        _create_catalog_view(
            conn,
            table_name="xhs_note_keyword_hit",
            key_columns=("note_id", "keyword", "task_id"),
            attached_sources=attached,
        )
        _create_catalog_view(
            conn,
            table_name="xhs_note_observation",
            key_columns=("run_id", "note_id", "keyword", "observation_kind"),
            attached_sources=attached,
        )
        _create_catalog_view(
            conn,
            table_name="xhs_comment_observation",
            key_columns=("run_id", "comment_id", "observation_kind"),
            attached_sources=attached,
        )
        return conn
    except Exception:
        conn.close()
        raise


def catalog_counts(*, task_db: Path | str = TASK_DB) -> Dict[str, int]:
    conn = open_content_catalog(task_db=task_db)
    try:
        result: Dict[str, int] = {}
        for table_name, key in (
            ("xhs_note", "notes"),
            ("xhs_note_comment", "comments"),
            ("xhs_note_keyword_hit", "keyword_hits"),
            ("xhs_note_observation", "note_observations"),
            ("xhs_comment_observation", "comment_observations"),
        ):
            exists = conn.execute(
                "SELECT 1 FROM sqlite_temp_master WHERE type='view' AND name=?",
                (table_name,),
            ).fetchone()
            result[key] = (
                int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])
                if exists
                else 0
            )
        return result
    finally:
        conn.close()


def mark_account_store_legacy_seeded(
    account_id: str,
    *,
    seed_cutoff: Optional[float] = None,
    task_db: Path | str = TASK_DB,
) -> Dict[str, Any]:
    """Record that a working DB began as a full legacy aggregate copy."""
    conn = _connect(task_db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT active_store_id FROM xhs_accounts WHERE account_id=?",
            (account_id,),
        ).fetchone()
        legacy = conn.execute(
            "SELECT store_id FROM xhs_data_stores "
            "WHERE store_kind='legacy_aggregate' ORDER BY priority DESC LIMIT 1"
        ).fetchone()
        if not row or not row[0]:
            raise XhsDataModelError(f"working store not found for account {account_id}")
        if not legacy:
            raise XhsDataModelError("legacy aggregate store is not registered")
        conn.execute(
            "UPDATE xhs_data_stores SET store_kind='legacy_seeded_working', "
            "seed_store_id=?, seed_cutoff=?, updated_at=? WHERE store_id=?",
            (legacy[0], seed_cutoff or time.time(), time.time(), row[0]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return route_metadata(account_id, task_db=task_db)


def source_paths(sources: Iterable[Dict[str, Any]]) -> list[str]:
    """Return catalog paths without treating a source label as an identity."""
    return [str(row["sqlite_db_path"]) for row in sources]
