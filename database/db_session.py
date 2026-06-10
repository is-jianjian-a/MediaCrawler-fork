# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/database/db_session.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#
# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给平台带来不必要的负担。
# 5. 不得用于任何非法或不当用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from contextlib import asynccontextmanager
from .models import Base
import config
from config.db_config import mysql_db_config, sqlite_db_config, postgres_db_config

_engines = {}
_session_factories = {}
_sqlite_lock = asyncio.Lock()

logger = logging.getLogger("MediaCrawler")


async def create_database_if_not_exists(db_type: str):
    if db_type == "mysql" or db_type == "db":
        server_url = f"mysql+asyncmy://{mysql_db_config['user']}:{mysql_db_config['password']}@{mysql_db_config['host']}:{mysql_db_config['port']}"
        engine = create_async_engine(server_url, echo=False)
        async with engine.connect() as conn:
            await conn.execute(text(f"CREATE DATABASE IF NOT EXISTS {mysql_db_config['db_name']}"))
        await engine.dispose()
    elif db_type == "postgres":
        server_url = f"postgresql+asyncpg://{postgres_db_config['user']}:{postgres_db_config['password']}@{postgres_db_config['host']}:{postgres_db_config['port']}/postgres"
        print(f"[init_db] Connecting to Postgres: host={postgres_db_config['host']}, port={postgres_db_config['port']}, user={postgres_db_config['user']}, dbname=postgres")
        engine = create_async_engine(server_url, echo=False, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            result = await conn.execute(text(f"SELECT 1 FROM pg_database WHERE datname = '{postgres_db_config['db_name']}'"))
            if not result.scalar():
                await conn.execute(text(f"CREATE DATABASE {postgres_db_config['db_name']}"))
        await engine.dispose()


def get_async_engine(db_type: str = None):
    if db_type is None:
        db_type = config.SAVE_DATA_OPTION

    if db_type in _engines:
        return _engines[db_type]

    if db_type in ["json", "jsonl", "csv"]:
        return None

    if db_type == "sqlite":
        db_url = f"sqlite+aiosqlite:///{sqlite_db_config['db_path']}?check_same_thread=false&timeout=30"
    elif db_type == "mysql" or db_type == "db":
        db_url = f"mysql+asyncmy://{mysql_db_config['user']}:{mysql_db_config['password']}@{mysql_db_config['host']}:{mysql_db_config['port']}/{mysql_db_config['db_name']}"
    elif db_type == "postgres":
        db_url = f"postgresql+asyncpg://{postgres_db_config['user']}:{postgres_db_config['password']}@{postgres_db_config['host']}:{postgres_db_config['port']}/{postgres_db_config['db_name']}"
    else:
        raise ValueError(f"Unsupported database type: {db_type}")

    engine = create_async_engine(db_url, echo=False, connect_args={"timeout": 30} if db_type == "sqlite" else {})
    _engines[db_type] = engine
    return engine


def _get_session_factory(db_type: str = None) -> sessionmaker:
    if db_type is None:
        db_type = config.SAVE_DATA_OPTION

    if db_type in _session_factories:
        return _session_factories[db_type]

    engine = get_async_engine(db_type)
    if not engine:
        return None

    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    _session_factories[db_type] = factory
    return factory


async def _enable_sqlite_wal(engine):
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("PRAGMA journal_mode"))
            current_mode = result.scalar()
            if current_mode != "wal":
                await conn.execute(text("PRAGMA journal_mode=WAL"))
                logger.info(f"[db_session] SQLite journal_mode changed from '{current_mode}' to 'WAL'")
            else:
                logger.info("[db_session] SQLite journal_mode is already WAL")
            await conn.execute(text("PRAGMA busy_timeout=30000"))
    except Exception as e:
        logger.warning(f"[db_session] Failed to enable SQLite WAL mode: {e}")


async def create_tables(db_type: str = None):
    if db_type is None:
        db_type = config.SAVE_DATA_OPTION
    await create_database_if_not_exists(db_type)
    engine = get_async_engine(db_type)
    if engine:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        if db_type == "sqlite":
            await _enable_sqlite_wal(engine)


@asynccontextmanager
async def get_session() -> AsyncSession:
    engine = get_async_engine(config.SAVE_DATA_OPTION)
    if not engine:
        yield None
        return

    is_sqlite = config.SAVE_DATA_OPTION == "sqlite"
    if is_sqlite:
        await _sqlite_lock.acquire()

    factory = _get_session_factory(config.SAVE_DATA_OPTION)
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception as e:
        await session.rollback()
        raise e
    finally:
        await session.close()
        if is_sqlite:
            _sqlite_lock.release()
