"""Database engines must be scoped by the resolved store, not only by type."""

import pytest

from database import db_session


@pytest.mark.asyncio
async def test_sqlite_engine_cache_changes_with_account_store(monkeypatch, tmp_path):
    monkeypatch.setattr(db_session, "_engines", {})
    monkeypatch.setattr(db_session, "_session_factories", {})

    first_path = tmp_path / "accounts" / "A" / "content.db"
    second_path = tmp_path / "accounts" / "B" / "content.db"
    monkeypatch.setitem(db_session.sqlite_db_config, "db_path", str(first_path))
    first = db_session.get_async_engine("sqlite")
    monkeypatch.setitem(db_session.sqlite_db_config, "db_path", str(second_path))
    second = db_session.get_async_engine("sqlite")

    try:
        assert first is not second
        assert str(first.url).split("?")[0].endswith(str(first_path))
        assert str(second.url).split("?")[0].endswith(str(second_path))
    finally:
        await first.dispose()
        await second.dispose()
