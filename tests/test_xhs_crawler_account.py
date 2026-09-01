# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tests/test_xhs_crawler_account.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#
# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

"""
针对多账号库合并为主库特性（feature/merge-account-db）的回归测试：

1. get_current_account() 在设置 / 未设置 MEDIACRAWLER_ACCOUNT 时的返回值；
2. XhsNote / XhsNoteComment 模型声明了 crawler_account 列；
3. 存储写入路径真实落库，验证取值规则
   crawler_account = content_item.get("crawler_account") or get_current_account()，
   且 update_content / update_comment 不会覆盖该字段。

所有写入测试均使用独立的临时 SQLite 库，不触碰生产数据库 sqlite_tables.db。
异步测试遵循本仓库约定：模块级 async 函数 + @pytest.mark.asyncio。
"""

import os
import tempfile

import pytest
from sqlalchemy import create_engine, select, inspect as sa_inspect
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from config.runtime_paths import LEGACY_CONTENT_DB

from database.models import (
    Base,
    XhsCommentObservation,
    XhsNote,
    XhsNoteComment,
    XhsNoteObservation,
)
from store.xhs._store_impl import XhsDbStoreImplement
from config.db_config import get_current_account as cfg_get_current_account


# ---------------------------------------------------------------------------
# 1. get_current_account() 解析逻辑
# ---------------------------------------------------------------------------

class TestGetCurrentAccount:
    def test_default_when_env_unset(self, monkeypatch):
        monkeypatch.setattr("config.db_config._DEFAULT_ACCOUNT", "")
        assert cfg_get_current_account() == "default"

    def test_returns_account_when_env_set(self, monkeypatch):
        monkeypatch.setattr("config.db_config._DEFAULT_ACCOUNT", "03")
        assert cfg_get_current_account() == "03"

    def test_explicit_default_value(self, monkeypatch):
        monkeypatch.setattr("config.db_config._DEFAULT_ACCOUNT", "default")
        assert cfg_get_current_account() == "default"


# ---------------------------------------------------------------------------
# 2. 模型列声明（无需连接数据库）
# ---------------------------------------------------------------------------

class TestModelColumns:
    def test_xhs_note_has_crawler_account(self):
        cols = {c.name for c in sa_inspect(XhsNote).columns}
        assert "crawler_account" in cols

    def test_xhs_note_comment_has_crawler_account(self):
        cols = {c.name for c in sa_inspect(XhsNoteComment).columns}
        assert "crawler_account" in cols

    def test_crawler_account_is_string(self):
        col = sa_inspect(XhsNote).columns["crawler_account"]
        assert col.type.python_type is str


# ---------------------------------------------------------------------------
# 3. 存储写入路径（独立临时 SQLite 库，真实落库）
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_db():
    """创建基于临时文件的 SQLite 库（同步建表 + 异步会话），不触碰生产库。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # 用同步引擎建表（与异步引擎指向同一文件）
    sync_engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    session = maker()
    yield session, engine, path
    try:
        os.remove(path)
    except OSError:
        pass


@pytest.mark.asyncio
async def test_store_add_content_falls_back_to_current_account(temp_db, monkeypatch):
    session, engine, path = temp_db
    try:
        monkeypatch.setattr("config.db_config._DEFAULT_ACCOUNT", "02")
        impl = XhsDbStoreImplement()
        item = {"note_id": "n_default", "user_id": "u1", "title": "t"}
        await impl.add_content(session, item)
        await session.commit()
        row = (await session.execute(
            select(XhsNote).where(XhsNote.note_id == "n_default"))).scalar_one()
        assert row.crawler_account == "02"
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_store_add_content_uses_explicit_crawler_account(temp_db, monkeypatch):
    session, engine, path = temp_db
    try:
        monkeypatch.setattr("config.db_config._DEFAULT_ACCOUNT", "02")
        impl = XhsDbStoreImplement()
        item = {"note_id": "n_explicit", "user_id": "u2", "crawler_account": "03"}
        await impl.add_content(session, item)
        await session.commit()
        row = (await session.execute(
            select(XhsNote).where(XhsNote.note_id == "n_explicit"))).scalar_one()
        assert row.crawler_account == "03"
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_store_add_comment_resolves_account(temp_db, monkeypatch):
    session, engine, path = temp_db
    try:
        monkeypatch.setattr("config.db_config._DEFAULT_ACCOUNT", "01")
        impl = XhsDbStoreImplement()
        item = {"comment_id": "c1", "note_id": "n1", "user_id": "u3"}
        await impl.add_comment(session, item)
        await session.commit()
        row = (await session.execute(
            select(XhsNoteComment).where(XhsNoteComment.comment_id == "c1"))).scalar_one()
        assert row.crawler_account == "01"
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_store_update_content_preserves_crawler_account(temp_db, monkeypatch):
    session, engine, path = temp_db
    try:
        monkeypatch.setattr("config.db_config._DEFAULT_ACCOUNT", "02")
        impl = XhsDbStoreImplement()
        await impl.add_content(
            session,
            {"note_id": "n_upd", "user_id": "u4", "crawler_account": "03"},
        )
        await session.commit()
        # 后续仅更新计数类字段，不应覆盖 crawler_account
        await impl.update_content(session, {"note_id": "n_upd", "liked_count": "999"})
        await session.commit()
        row = (await session.execute(
            select(XhsNote).where(XhsNote.note_id == "n_upd"))).scalar_one()
        assert row.crawler_account == "03"
        assert row.liked_count == 999
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_run_observations_preserve_each_account_sighting(temp_db, monkeypatch):
    session, engine, path = temp_db
    try:
        monkeypatch.setattr("config.db_config._DEFAULT_ACCOUNT", "02")
        monkeypatch.setenv("MEDIACRAWLER_RUN_ID", "run-02-a")
        monkeypatch.setenv("MEDIACRAWLER_TASK_ID", "crawl-a")
        monkeypatch.setenv("MEDIACRAWLER_PROFILE_ID", "profile-02")
        monkeypatch.setenv("MEDIACRAWLER_STORE_ID", "store-02")
        monkeypatch.setenv("MEDIACRAWLER_ROUTE_ID", "route-02")
        impl = XhsDbStoreImplement()
        note_item = {
            "note_id": "n-observed",
            "source_keyword": "地铁走神",
            "raw_data": '{"id":"n-observed"}',
        }
        comment_item = {
            "comment_id": "c-observed",
            "note_id": "n-observed",
            "parent_comment_id": "",
            "raw_data": '{"id":"c-observed"}',
        }
        await impl.record_note_observation(session, note_item)
        await impl.record_note_observation(session, note_item)
        await impl.record_note_observation(
            session, {**note_item, "source_keyword": "地铁换乘"}
        )
        await impl.record_comment_observation(session, comment_item)
        await session.commit()

        note = (
            await session.execute(
                select(XhsNoteObservation).where(
                    XhsNoteObservation.note_id == "n-observed",
                    XhsNoteObservation.keyword == "地铁走神",
                )
            )
        ).scalar_one()
        comment = (
            await session.execute(
                select(XhsCommentObservation).where(
                    XhsCommentObservation.comment_id == "c-observed"
                )
            )
        ).scalar_one()
        assert note.run_id == "run-02-a"
        assert note.account_id == "02"
        assert note.profile_id == "profile-02"
        assert note.store_id == "store-02"
        assert note.route_id == "route-02"
        assert note.seen_count == 2
        all_note_observations = (
            await session.execute(
                select(XhsNoteObservation).where(
                    XhsNoteObservation.note_id == "n-observed"
                )
            )
        ).scalars().all()
        assert {row.keyword for row in all_note_observations} == {
            "地铁走神",
            "地铁换乘",
        }
        assert comment.run_id == "run-02-a"
        assert comment.note_id == "n-observed"
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_untracked_write_does_not_create_fake_observation(
    temp_db, monkeypatch
):
    session, engine, path = temp_db
    try:
        monkeypatch.delenv("MEDIACRAWLER_RUN_ID", raising=False)
        impl = XhsDbStoreImplement()
        await impl.record_note_observation(session, {"note_id": "n-legacy"})
        await session.commit()
        count = (
            await session.execute(select(XhsNoteObservation))
        ).scalars().all()
        assert count == []
    finally:
        await session.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# 4. 生产库一致性（只读，缺失则跳过）
# ---------------------------------------------------------------------------

REAL_DB = str(LEGACY_CONTENT_DB)


@pytest.mark.skipif(not os.path.exists(REAL_DB), reason="生产内容库不存在")
class TestRealDatabaseConsistency:
    def test_crawler_account_column_present_and_populated(self):
        import sqlite3

        con = sqlite3.connect(REAL_DB)
        cur = con.cursor()
        try:
            for tbl in ("xhs_note", "xhs_note_comment"):
                cols = [r[1] for r in cur.execute(f"PRAGMA table_info({tbl})")]
                assert "crawler_account" in cols, f"{tbl} 缺少 crawler_account 列"

                total = cur.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                non_default = cur.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE crawler_account <> 'default'"
                ).fetchone()[0]
                assert total > 0, f"{tbl} 无数据"
                assert non_default > 0, f"{tbl} 全部为 default，账号迁移疑似失效"
        finally:
            con.close()
