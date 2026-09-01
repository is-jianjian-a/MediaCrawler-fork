"""WAL-aware inspection and backup helpers for MediaCrawler SQLite stores."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict


class SqliteMaintenanceError(RuntimeError):
    """Raised when a SQLite store cannot be inspected or backed up safely."""


def _read_only_uri(path: Path) -> str:
    return path.resolve(strict=True).as_uri() + "?mode=ro"


def inspect_sqlite_database(
    db_path: Path | str,
    *,
    run_quick_check: bool = False,
) -> Dict[str, Any]:
    """Inspect the live database through SQLite so active WAL data is visible."""
    path = Path(db_path).expanduser().resolve(strict=True)
    conn = sqlite3.connect(_read_only_uri(path), uri=True, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        latest_write_ms = 0
        for table_name in ("xhs_note", "xhs_note_comment"):
            if table_name not in tables:
                continue
            row = conn.execute(f"SELECT MAX(add_ts) FROM {table_name}").fetchone()
            latest_write_ms = max(latest_write_ms, int((row or [0])[0] or 0))
        quick_check = "not_run"
        if run_quick_check:
            quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        conn.close()

    wal_path = Path(f"{path}-wal")
    shm_path = Path(f"{path}-shm")
    return {
        "exists": True,
        "size_bytes": path.stat().st_size,
        "wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
        "shm_bytes": shm_path.stat().st_size if shm_path.exists() else 0,
        "journal_mode": journal_mode,
        "page_count": page_count,
        "page_size": page_size,
        "logical_size_bytes": page_count * page_size,
        "latest_write_ms": latest_write_ms,
        "quick_check": quick_check,
    }


def backup_sqlite_database(
    source_path: Path | str,
    destination_path: Path | str,
    *,
    replace: bool = False,
) -> Dict[str, Any]:
    """Create an atomic, WAL-aware SQLite backup and verify its integrity."""
    source = Path(source_path).expanduser().resolve(strict=True)
    destination = Path(destination_path).expanduser().resolve(strict=False)
    if source == destination:
        raise SqliteMaintenanceError("backup destination must differ from source")
    if destination.exists() and not replace:
        raise SqliteMaintenanceError(f"backup destination already exists: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{int(time.time() * 1000)}.tmp"
    )
    temporary.unlink(missing_ok=True)
    source_conn = sqlite3.connect(_read_only_uri(source), uri=True, timeout=30)
    target_conn = sqlite3.connect(str(temporary), timeout=30)
    try:
        source_conn.execute("PRAGMA busy_timeout=30000")
        source_conn.backup(target_conn)
        target_conn.commit()
        integrity = str(target_conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise SqliteMaintenanceError(
                f"backup failed integrity_check: {integrity}"
            )
    except Exception:
        target_conn.close()
        source_conn.close()
        temporary.unlink(missing_ok=True)
        raise
    else:
        target_conn.close()
        source_conn.close()

    if destination.exists() and replace:
        destination.unlink()
    os.replace(temporary, destination)
    result = inspect_sqlite_database(destination, run_quick_check=True)
    result.update({"destination": str(destination), "source": str(source)})
    return result
