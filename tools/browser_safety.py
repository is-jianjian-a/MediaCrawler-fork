"""Shared safety checks for browser executables used by automation."""

from __future__ import annotations

import os
from pathlib import Path


SYSTEM_GOOGLE_CHROME = Path(
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)


class BrowserPathError(ValueError):
    """Raised when a browser executable is missing or unsafe for automation."""


def validate_automation_browser_path(
    browser_path: str,
    *,
    require_exists: bool = True,
    required: bool = True,
) -> str:
    """Resolve an automation browser and reject the user's daily macOS Chrome."""
    raw_path = str(browser_path or "").strip()
    if not raw_path:
        if required:
            raise BrowserPathError("an explicit isolated browser_path is required")
        return ""

    candidate = Path(raw_path).expanduser()
    try:
        resolved = candidate.resolve(strict=require_exists)
    except (OSError, RuntimeError) as exc:
        raise BrowserPathError(
            f"isolated browser_path does not exist: {candidate}"
        ) from exc

    system_chrome = SYSTEM_GOOGLE_CHROME.resolve(strict=False)
    if resolved == system_chrome or system_chrome.parent.parent in resolved.parents:
        raise BrowserPathError(
            "system Google Chrome is forbidden for automated tasks"
        )
    if require_exists and (not resolved.is_file() or not os.access(resolved, os.X_OK)):
        raise BrowserPathError(
            f"isolated browser_path is not executable: {resolved}"
        )
    return str(resolved)
