#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-process lock for one Xiaohongshu browser profile.

The Dashboard scheduler is advisory.  This OS lock prevents two workers from
opening the same persistent Chromium profile even when two Dashboard processes
or a stale lease race with each other.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import socket
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO

try:
    from dashboard.account_registry import resolve_profile_path
except ModuleNotFoundError:  # Support direct script execution.
    from account_registry import resolve_profile_path  # type: ignore[no-redef]


LOCK_DIR = Path(__file__).resolve().parent / "locks"


class ProfileLockError(RuntimeError):
    """Raised when another worker already owns the browser profile."""


def native_profile_owner(user_data_dir: str) -> dict:
    """Return a live Chromium SingletonLock owner, including manual login windows."""
    profile_path = resolve_profile_path(user_data_dir)
    singleton_lock = profile_path / "SingletonLock"
    if not singleton_lock.is_symlink():
        return {}
    try:
        target = os.readlink(singleton_lock)
    except OSError:
        return {}
    match = re.search(r"-(\d+)$", target)
    if not match:
        return {
            "in_use": True,
            "pid": 0,
            "owner": target,
            "reason": "Chromium profile has an unrecognized native lock",
        }
    pid = int(match.group(1))
    owner_host = target[: match.start()]
    local_hosts = {socket.gethostname(), socket.getfqdn()}
    if owner_host and owner_host not in local_hosts:
        return {
            "in_use": True,
            "pid": pid,
            "owner": target,
            "reason": "Chromium profile is locked by another host",
        }
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return {}
    except PermissionError:
        pass
    return {
        "in_use": True,
        "pid": pid,
        "owner": target,
        "reason": "Chromium profile is already open",
    }


def _lock_path(user_data_dir: str) -> Path:
    profile_path = resolve_profile_path(user_data_dir)
    digest = hashlib.sha256(str(profile_path).encode("utf-8")).hexdigest()[:24]
    return LOCK_DIR / f"xhs-profile-{digest}.lock"


def _read_metadata(handle: TextIO) -> dict:
    try:
        handle.seek(0)
        payload = json.loads(handle.read() or "{}")
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_metadata(handle: TextIO, payload: dict) -> None:
    handle.seek(0)
    handle.truncate()
    json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())


@contextmanager
def acquire_profile_lock(
    *,
    account_id: str,
    user_data_dir: str,
    task_id: str,
) -> Iterator[Path]:
    """Hold an exclusive lock until the worker and its browser have exited."""
    profile_path = resolve_profile_path(user_data_dir)
    lock_path = _lock_path(user_data_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            owner = _read_metadata(handle)
            owner_text = owner.get("task_id") or owner.get("pid") or "unknown"
            raise ProfileLockError(
                f"browser profile is already locked by {owner_text}"
            ) from exc

        native_owner = native_profile_owner(user_data_dir)
        if native_owner:
            raise ProfileLockError(
                f"{native_owner['reason']} (pid={native_owner.get('pid') or 'unknown'})"
            )

        _write_metadata(
            handle,
            {
                "account_id": account_id,
                "task_id": task_id,
                "pid": os.getpid(),
                "profile_path": str(profile_path),
                "acquired_at": time.time(),
            },
        )
        yield profile_path
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
