#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persistent Xiaohongshu account registry for Dashboard-managed workers.

The registry intentionally stores only runtime routing metadata. Login cookies
and other credentials remain inside each account's isolated browser profile.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


MEDIACRAWLER_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = MEDIACRAWLER_ROOT / "dashboard"
TASK_DB = DASHBOARD_DIR / "database" / "task_manager.db"
BROWSER_DATA_ROOT = MEDIACRAWLER_ROOT / "browser_data"
ACCOUNT_DATA_ROOT = MEDIACRAWLER_ROOT / "database" / "accounts"
LEGACY_CONTENT_DB = MEDIACRAWLER_ROOT / "database" / "sqlite_tables.db"

DEFAULT_ACCOUNT_ID = os.getenv("MEDIACRAWLER_DEFAULT_XHS_ACCOUNT", "02")
DEFAULT_USER_DATA_DIR = "%s_user_data_dir_account02"
SYSTEM_CHROME_PATH = Path(
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)
ACCOUNT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_INITIALIZED_DATABASES: set[str] = set()


class AccountRegistryError(ValueError):
    """Raised when account routing would weaken isolation."""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(TASK_DB), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def validate_account_id(account_id: str) -> str:
    normalized = str(account_id or "").strip()
    if not ACCOUNT_ID_PATTERN.fullmatch(normalized):
        raise AccountRegistryError(
            "account_id must be 1-32 characters using letters, numbers, _ or -"
        )
    return normalized


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def resolve_profile_path(user_data_dir: str) -> Path:
    """Resolve a profile template and guarantee it stays under browser_data."""
    template = str(user_data_dir or "").strip()
    if template.count("%s") != 1:
        raise AccountRegistryError("user_data_dir must contain exactly one %s placeholder")
    if Path(template).is_absolute():
        raise AccountRegistryError("user_data_dir must be relative to browser_data")
    try:
        relative = template % "xhs"
    except (TypeError, ValueError) as exc:
        raise AccountRegistryError("invalid user_data_dir template") from exc
    profile_path = (BROWSER_DATA_ROOT / relative).resolve(strict=False)
    browser_root = BROWSER_DATA_ROOT.resolve(strict=False)
    if profile_path == browser_root or not _is_relative_to(profile_path, browser_root):
        raise AccountRegistryError("browser profile must stay inside browser_data")
    return profile_path


def validate_browser_path(browser_path: str, *, require_exists: bool = True) -> str:
    raw = str(browser_path or "").strip()
    if not raw:
        raise AccountRegistryError("browser_path is required")
    candidate = Path(raw).expanduser()
    try:
        resolved = candidate.resolve(strict=require_exists)
    except (OSError, RuntimeError) as exc:
        raise AccountRegistryError(f"isolated browser_path does not exist: {candidate}") from exc
    system_chrome = SYSTEM_CHROME_PATH.resolve(strict=False)
    if resolved == system_chrome or str(system_chrome.parent.parent) in str(resolved):
        raise AccountRegistryError("system Google Chrome is forbidden for automated tasks")
    if require_exists and (not resolved.is_file() or not os.access(resolved, os.X_OK)):
        raise AccountRegistryError(f"isolated browser_path is not executable: {resolved}")
    return str(resolved)


def _validate_content_db_path(
    db_path: str,
    *,
    storage_mode: str,
) -> str:
    candidate = Path(str(db_path or "")).expanduser()
    if not candidate.is_absolute():
        candidate = MEDIACRAWLER_ROOT / candidate
    resolved = candidate.resolve(strict=False)
    if storage_mode == "legacy_shared" and resolved == LEGACY_CONTENT_DB.resolve(strict=False):
        return str(resolved)
    account_root = ACCOUNT_DATA_ROOT.resolve(strict=False)
    if not _is_relative_to(resolved, account_root):
        raise AccountRegistryError(
            "dedicated account database must stay inside database/accounts"
        )
    return str(resolved)


def default_user_data_dir(account_id: str) -> str:
    account_id = validate_account_id(account_id)
    return f"%s_user_data_dir_account{account_id}"


def default_content_db_path(account_id: str) -> str:
    account_id = validate_account_id(account_id)
    return str((ACCOUNT_DATA_ROOT / account_id / "sqlite_tables.db").resolve(strict=False))


def _row_to_account(row: sqlite3.Row) -> Dict[str, Any]:
    account = dict(row)
    account["enabled"] = bool(account.get("enabled"))
    account["profile_path"] = str(resolve_profile_path(account["user_data_dir"]))
    account["profile_exists"] = Path(account["profile_path"]).exists()
    account["content_db_exists"] = Path(account["sqlite_db_path"]).exists()
    return account


def _iter_historical_task_configs(conn: sqlite3.Connection) -> Iterable[dict]:
    for table_name in ("crawl_tasks", "tasks"):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        if not exists:
            continue
        for row in conn.execute(
            f"SELECT config_json FROM {table_name} WHERE config_json IS NOT NULL "
            "ORDER BY created_at DESC"
        ).fetchall():
            try:
                config = json.loads(row[0] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(config, dict):
                yield config


def _historical_browser_path(conn: sqlite3.Connection, user_data_dir: str) -> str:
    configured = str(os.getenv("MEDIACRAWLER_BROWSER_PATH", "") or "").strip()
    for config in _iter_historical_task_configs(conn):
        if str(config.get("user_data_dir", "") or "") != user_data_dir:
            continue
        candidate = str(config.get("browser_path", "") or "").strip()
        if candidate:
            return candidate
    return configured


def _infer_account_id(user_data_dir: str) -> str:
    profile = str(user_data_dir or "").strip()
    match = re.search(r"account([A-Za-z0-9_-]{1,32})", profile)
    if match:
        return validate_account_id(match.group(1))
    if profile in ("", "%s_user_data_dir"):
        return "legacy-default"
    digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:8]
    return f"legacy-{digest}"


def _insert_account_if_missing(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    display_name: str,
    user_data_dir: str,
    browser_path: str,
    sqlite_db_path: str,
    storage_mode: str,
    enabled: bool,
) -> None:
    now = time.time()
    conn.execute(
        """
        INSERT OR IGNORE INTO xhs_accounts
        (account_id, display_name, user_data_dir, browser_path, sqlite_db_path,
         storage_mode, enabled, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            account_id,
            display_name,
            user_data_dir,
            browser_path,
            sqlite_db_path,
            storage_mode,
            1 if enabled else 0,
            now,
            now,
        ),
    )


def _backfill_task_accounts(conn: sqlite3.Connection) -> None:
    for table_name in ("crawl_tasks", "tasks"):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        if not exists:
            continue
        columns = {
            row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if "account_id" not in columns:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN account_id TEXT")
        rows = conn.execute(
            f"SELECT id, account_id, config_json FROM {table_name}"
        ).fetchall()
        for task_id, stored_account_id, config_json in rows:
            if stored_account_id:
                continue
            try:
                config = json.loads(config_json or "{}")
            except (TypeError, json.JSONDecodeError):
                config = {}
            requested_id = str(config.get("account_id", "") or "").strip()
            profile = str(config.get("user_data_dir", "") or "").strip()
            account_id = requested_id or _infer_account_id(profile)
            existing = conn.execute(
                "SELECT 1 FROM xhs_accounts WHERE account_id=?", (account_id,)
            ).fetchone()
            if not existing:
                user_data_dir = profile or default_user_data_dir(account_id)
                try:
                    resolve_profile_path(user_data_dir)
                except AccountRegistryError:
                    # Historical routing metadata can be malformed or absent.
                    # Preserve the task's inferred identity but quarantine it
                    # behind a valid, disabled placeholder profile.
                    user_data_dir = default_user_data_dir(account_id)
                browser_path = str(config.get("browser_path", "") or "").strip()
                try:
                    browser_path = validate_browser_path(browser_path)
                except AccountRegistryError:
                    browser_path = ""
                _insert_account_if_missing(
                    conn,
                    account_id=account_id,
                    display_name=f"历史账号 {account_id}",
                    user_data_dir=user_data_dir,
                    browser_path=browser_path,
                    sqlite_db_path=str(LEGACY_CONTENT_DB.resolve(strict=False)),
                    storage_mode="legacy_shared",
                    # Historical rows share the legacy content DB and do not
                    # carry complete per-account provenance.  Keep them
                    # visible for audit, but never auto-enable them as a new
                    # concurrent worker route.
                    enabled=False,
                )
            config["account_id"] = account_id
            conn.execute(
                f"UPDATE {table_name} SET account_id=?, config_json=? WHERE id=?",
                (account_id, json.dumps(config, ensure_ascii=False), task_id),
            )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table_name}_account_status "
            f"ON {table_name}(account_id, status)"
        )


def init_account_registry_db() -> None:
    database_key = str(TASK_DB.resolve(strict=False))
    if database_key in _INITIALIZED_DATABASES:
        return
    TASK_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS xhs_accounts (
            account_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            user_data_dir TEXT NOT NULL UNIQUE,
            browser_path TEXT NOT NULL,
            sqlite_db_path TEXT NOT NULL,
            storage_mode TEXT NOT NULL DEFAULT 'dedicated',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            CHECK (storage_mode IN ('dedicated', 'legacy_shared'))
        )
        """
    )
    default_browser = _historical_browser_path(conn, DEFAULT_USER_DATA_DIR)
    try:
        default_browser = validate_browser_path(default_browser)
    except AccountRegistryError:
        default_browser = ""
    _insert_account_if_missing(
        conn,
        account_id=DEFAULT_ACCOUNT_ID,
        display_name=f"小红书账号 {DEFAULT_ACCOUNT_ID}",
        user_data_dir=DEFAULT_USER_DATA_DIR,
        browser_path=default_browser,
        sqlite_db_path=str(LEGACY_CONTENT_DB.resolve(strict=False)),
        storage_mode="legacy_shared",
        enabled=bool(default_browser),
    )
    _backfill_task_accounts(conn)
    conn.commit()
    conn.close()
    _INITIALIZED_DATABASES.add(database_key)


def list_accounts(*, include_disabled: bool = True) -> list[Dict[str, Any]]:
    init_account_registry_db()
    conn = _connect()
    if include_disabled:
        rows = conn.execute(
            "SELECT * FROM xhs_accounts ORDER BY created_at, account_id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM xhs_accounts WHERE enabled=1 ORDER BY created_at, account_id"
        ).fetchall()
    conn.close()
    return [_row_to_account(row) for row in rows]


def get_account(
    account_id: Optional[str] = None,
    *,
    require_enabled: bool = False,
) -> Optional[Dict[str, Any]]:
    init_account_registry_db()
    normalized = validate_account_id(account_id or DEFAULT_ACCOUNT_ID)
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM xhs_accounts WHERE account_id=?", (normalized,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    account = _row_to_account(row)
    if require_enabled and not account["enabled"]:
        raise AccountRegistryError(f"account {normalized} is disabled")
    return account


def create_account(
    *,
    account_id: str,
    display_name: str = "",
    browser_path: str,
    user_data_dir: str = "",
    enabled: bool = False,
) -> Dict[str, Any]:
    init_account_registry_db()
    account_id = validate_account_id(account_id)
    profile_template = str(user_data_dir or default_user_data_dir(account_id)).strip()
    profile_path = resolve_profile_path(profile_template)
    isolated_browser = validate_browser_path(browser_path)
    content_db = _validate_content_db_path(
        default_content_db_path(account_id), storage_mode="dedicated"
    )
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        duplicate = conn.execute(
            "SELECT account_id FROM xhs_accounts WHERE account_id=? OR user_data_dir=?",
            (account_id, profile_template),
        ).fetchone()
        if duplicate:
            raise AccountRegistryError(
                f"account or browser profile already registered: {duplicate[0]}"
            )
        for row in conn.execute(
            "SELECT account_id, user_data_dir FROM xhs_accounts"
        ).fetchall():
            try:
                existing_profile_path = resolve_profile_path(row["user_data_dir"])
            except AccountRegistryError:
                continue
            if existing_profile_path == profile_path:
                raise AccountRegistryError(
                    f"physical browser profile already assigned to account {row['account_id']}"
                )
        shared_db = conn.execute(
            "SELECT account_id FROM xhs_accounts WHERE sqlite_db_path=? AND enabled=1",
            (content_db,),
        ).fetchone()
        if shared_db:
            raise AccountRegistryError(
                f"content database already assigned to account {shared_db[0]}"
            )
        now = time.time()
        conn.execute(
            """
            INSERT INTO xhs_accounts
            (account_id, display_name, user_data_dir, browser_path, sqlite_db_path,
             storage_mode, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'dedicated', ?, ?, ?)
            """,
            (
                account_id,
                str(display_name or f"小红书账号 {account_id}").strip()[:80],
                profile_template,
                isolated_browser,
                content_db,
                1 if enabled else 0,
                now,
                now,
            ),
        )
        Path(content_db).parent.mkdir(parents=True, exist_ok=True)
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_account(account_id)  # type: ignore[return-value]


def update_account(
    account_id: str,
    *,
    display_name: Optional[str] = None,
    browser_path: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    account_id = validate_account_id(account_id)
    account = get_account(account_id)
    if not account:
        raise AccountRegistryError(f"account not found: {account_id}")
    updates: dict[str, Any] = {}
    if display_name is not None:
        normalized_name = str(display_name).strip()
        if not normalized_name:
            raise AccountRegistryError("display_name must not be empty")
        updates["display_name"] = normalized_name[:80]
    if browser_path is not None:
        updates["browser_path"] = validate_browser_path(browser_path)
    if enabled is not None:
        if enabled and account["storage_mode"] != "dedicated" and account_id != DEFAULT_ACCOUNT_ID:
            raise AccountRegistryError(
                "historical shared-db accounts must be migrated to dedicated storage before enabling"
            )
        updates["enabled"] = 1 if enabled else 0
    if not updates:
        return account
    updates["updated_at"] = time.time()
    columns = ", ".join(f"{key}=?" for key in updates)
    conn = _connect()
    conn.execute(
        f"UPDATE xhs_accounts SET {columns} WHERE account_id=?",
        (*updates.values(), account_id),
    )
    conn.commit()
    conn.close()
    return get_account(account_id)  # type: ignore[return-value]


def bind_task_config(
    config: Optional[Dict[str, Any]],
    *,
    account_id: Optional[str] = None,
    require_enabled: bool = True,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Replace user-supplied routing values with the registry's immutable route."""
    normalized_config = dict(config or {})
    selected = account_id or normalized_config.get("account_id") or DEFAULT_ACCOUNT_ID
    account = get_account(str(selected), require_enabled=require_enabled)
    if not account:
        raise AccountRegistryError(f"account not found: {selected}")
    normalized_config.update(
        {
            "account_id": account["account_id"],
            "user_data_dir": account["user_data_dir"],
            "browser_path": account["browser_path"],
            "sqlite_db_path": account["sqlite_db_path"],
        }
    )
    return normalized_config, account


def account_db_path(account_id: Optional[str] = None) -> str:
    account = get_account(account_id or DEFAULT_ACCOUNT_ID)
    if not account:
        raise AccountRegistryError(f"account not found: {account_id}")
    return str(account["sqlite_db_path"])


def _public_account(account: Dict[str, Any]) -> Dict[str, Any]:
    """Return a CLI-safe summary without exposing absolute local paths."""
    return {
        "account_id": account["account_id"],
        "display_name": account["display_name"],
        "enabled": account["enabled"],
        "storage_mode": account["storage_mode"],
        "profile_exists": account["profile_exists"],
        "content_db_exists": account["content_db_exists"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage local isolated Xiaohongshu Dashboard accounts"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="List registered accounts")

    add_parser = subparsers.add_parser("add", help="Register one isolated account")
    add_parser.add_argument("account_id")
    add_parser.add_argument("--display-name", default="")
    add_parser.add_argument("--browser-path", required=True)
    add_parser.add_argument("--user-data-dir", default="")
    add_parser.add_argument(
        "--enable",
        action="store_true",
        help="Enable immediately only when this isolated profile is already logged in",
    )

    state_parser = subparsers.add_parser("set-enabled", help="Enable or disable an account")
    state_parser.add_argument("account_id")
    state_parser.add_argument("enabled", choices=("true", "false"))

    browser_parser = subparsers.add_parser(
        "set-browser", help="Replace the isolated browser executable"
    )
    browser_parser.add_argument("account_id")
    browser_parser.add_argument("browser_path")

    args = parser.parse_args()
    try:
        if args.command == "list":
            payload = [_public_account(account) for account in list_accounts()]
        elif args.command == "add":
            payload = _public_account(
                create_account(
                    account_id=args.account_id,
                    display_name=args.display_name,
                    browser_path=args.browser_path,
                    user_data_dir=args.user_data_dir,
                    enabled=args.enable,
                )
            )
        elif args.command == "set-enabled":
            payload = _public_account(
                update_account(args.account_id, enabled=args.enabled == "true")
            )
        else:
            payload = _public_account(
                update_account(args.account_id, browser_path=args.browser_path)
            )
    except AccountRegistryError as exc:
        parser.error(str(exc))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
