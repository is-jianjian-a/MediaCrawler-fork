# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/xhs/_store_impl.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#
# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

# @Author  : persist1@126.com
# @Time    : 2025/9/5 19:34
# @Desc    : Xiaohongshu storage implementation class
import hashlib
import json
import os
import re
from datetime import datetime
from typing import Dict, List, Set

from sqlalchemy import func, select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from base.base_crawler import AbstractStore
from database.db_session import get_session
from database.models import (
    XhsCommentObservation,
    XhsCreator,
    XhsNote,
    XhsNoteComment,
    XhsNoteKeywordHit,
    XhsNoteObservation,
)

from tools.async_file_writer import AsyncFileWriter
from tools.time_util import get_current_timestamp
from var import crawler_type_var
from database.mongodb_store_base import MongoDBStoreBase
from tools import utils
from store.excel_store_base import ExcelStoreBase
from config.db_config import get_current_account


def _parse_count(value) -> int:
    """Parse XHS API count values handling "X.X万" format.

    Examples:
        "2.6万" → 26000
        "1234"  → 1234
        None     → 0
    """
    if not value:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    value = str(value).strip()
    if not value:
        return 0
    match = re.match(r"^([\d.]+)万$", value)
    if match:
        return int(float(match.group(1)) * 10000)
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return 0


def _payload_hash(item: Dict) -> str:
    raw_data = item.get("raw_data")
    if isinstance(raw_data, str) and raw_data:
        payload = raw_data
    elif raw_data:
        payload = json.dumps(raw_data, ensure_ascii=False, sort_keys=True, default=str)
    else:
        payload = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def _runtime_provenance() -> Dict[str, str]:
    return {
        "run_id": str(os.getenv("MEDIACRAWLER_RUN_ID", "") or "").strip(),
        "task_id": str(os.getenv("MEDIACRAWLER_TASK_ID", "") or "").strip(),
        "account_id": str(get_current_account() or "default").strip(),
        "profile_id": str(os.getenv("MEDIACRAWLER_PROFILE_ID", "") or "").strip(),
        "store_id": str(os.getenv("MEDIACRAWLER_STORE_ID", "") or "").strip(),
        "route_id": str(os.getenv("MEDIACRAWLER_ROUTE_ID", "") or "").strip(),
    }


class XhsCsvStoreImplement(AbstractStore):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.writer = AsyncFileWriter(platform="xhs", crawler_type=crawler_type_var.get())

    async def store_content(self, content_item: Dict):
        """
        store content data to csv file
        :param content_item:
        :return:
        """
        await self.writer.write_to_csv(item_type="contents", item=content_item)

    async def store_comment(self, comment_item: Dict):
        """
        store comment data to csv file
        :param comment_item:
        :return:
        """
        await self.writer.write_to_csv(item_type="comments", item=comment_item)


    async def store_creator(self, creator_item: Dict):
        await self.writer.write_to_csv(item_type="creators", item=creator_item)

    def flush(self):
        pass


class XhsJsonStoreImplement(AbstractStore):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.writer = AsyncFileWriter(platform="xhs", crawler_type=crawler_type_var.get())

    async def store_content(self, content_item: Dict):
        """
        store content data to json file
        :param content_item:
        :return:
        """
        await self.writer.write_single_item_to_json(item_type="contents", item=content_item)

    async def store_comment(self, comment_item: Dict):
        """
        store comment data to json file
        :param comment_item:
        :return:
        """
        await self.writer.write_single_item_to_json(item_type="comments", item=comment_item)

    async def store_creator(self, creator_item: Dict):
        """
        store creator data to json file
        :param creator_item:
        :return:
        """
        await self.writer.write_single_item_to_json(item_type="creators", item=creator_item)

    def flush(self):
        """
        flush data to json file
        :return:
        """
        pass



class XhsJsonlStoreImplement(AbstractStore):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.writer = AsyncFileWriter(platform="xhs", crawler_type=crawler_type_var.get())

    async def store_content(self, content_item: Dict):
        await self.writer.write_to_jsonl(item_type="contents", item=content_item)

    async def store_comment(self, comment_item: Dict):
        await self.writer.write_to_jsonl(item_type="comments", item=comment_item)

    async def store_creator(self, creator_item: Dict):
        await self.writer.write_to_jsonl(item_type="creators", item=creator_item)

    def flush(self):
        pass


class XhsDbStoreImplement(AbstractStore):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def store_content(self, content_item: Dict):
        note_id = content_item.get("note_id")
        if not note_id:
            return
        async with get_session() as session:
            if await self.content_is_exist(session, note_id):
                await self.update_content(session, content_item)
            else:
                await self.add_content(session, content_item)
            await self.record_note_observation(session, content_item)

    async def record_note_observation(
        self,
        session: AsyncSession,
        content_item: Dict,
        *,
        observation_kind: str = "detail",
        keyword: str = "",
        search_page: int = 0,
        rank_in_page: int = 0,
    ) -> None:
        provenance = _runtime_provenance()
        run_id = provenance["run_id"]
        note_id = str(content_item.get("note_id") or "").strip()
        if not run_id or not note_id:
            return
        effective_keyword = str(
            keyword or content_item.get("keyword") or content_item.get("source_keyword") or ""
        )
        now_ts = int(get_current_timestamp())
        stmt = select(XhsNoteObservation).where(
            XhsNoteObservation.run_id == run_id,
            XhsNoteObservation.note_id == note_id,
            XhsNoteObservation.keyword == effective_keyword,
            XhsNoteObservation.observation_kind == observation_kind,
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
        values = {
            "task_id": provenance["task_id"],
            "account_id": provenance["account_id"],
            "profile_id": provenance["profile_id"],
            "store_id": provenance["store_id"],
            "route_id": provenance["route_id"],
            "keyword": effective_keyword,
            "observation_kind": observation_kind,
            "search_page": int(search_page or 0),
            "rank_in_page": int(rank_in_page or 0),
            "last_seen_ts": now_ts,
            "payload_hash": _payload_hash(content_item),
            "legacy_source_label": str(
                content_item.get("crawler_account") or provenance["account_id"]
            ),
        }
        if existing:
            await session.execute(
                update(XhsNoteObservation)
                .where(XhsNoteObservation.id == existing.id)
                .values(**values, seen_count=(existing.seen_count or 0) + 1)
            )
            return
        session.add(
            XhsNoteObservation(
                run_id=run_id,
                note_id=note_id,
                observed_at=now_ts,
                seen_count=1,
                **values,
            )
        )

    async def store_keyword_hit(self, hit_item: Dict):
        note_id = hit_item.get("note_id")
        keyword = hit_item.get("keyword")
        if not note_id or not keyword:
            return

        task_id = hit_item.get("task_id") or ""
        now_ts = int(get_current_timestamp())
        async with get_session() as session:
            stmt = select(XhsNoteKeywordHit).where(
                XhsNoteKeywordHit.note_id == note_id,
                XhsNoteKeywordHit.keyword == keyword,
                XhsNoteKeywordHit.task_id == task_id,
            )
            result = await session.execute(stmt)
            existing_hit = result.scalar_one_or_none()
            await self.record_note_observation(
                session,
                hit_item,
                observation_kind="search_hit",
                keyword=str(keyword),
                search_page=int(hit_item.get("search_page") or 0),
                rank_in_page=int(hit_item.get("rank_in_page") or 0),
            )
            if existing_hit:
                update_stmt = (
                    update(XhsNoteKeywordHit)
                    .where(XhsNoteKeywordHit.id == existing_hit.id)
                    .values(
                        search_page=int(hit_item.get("search_page") or 0),
                        rank_in_page=int(hit_item.get("rank_in_page") or 0),
                        last_seen_ts=now_ts,
                        hit_count=(existing_hit.hit_count or 0) + 1,
                    )
                )
                await session.execute(update_stmt)
                return

            session.add(
                XhsNoteKeywordHit(
                    note_id=note_id,
                    keyword=keyword,
                    task_id=task_id,
                    platform=hit_item.get("platform") or "xhs",
                    search_page=int(hit_item.get("search_page") or 0),
                    rank_in_page=int(hit_item.get("rank_in_page") or 0),
                    first_seen_ts=now_ts,
                    last_seen_ts=now_ts,
                    hit_count=1,
                )
            )

    async def add_content(self, session: AsyncSession, content_item: Dict):
        add_ts = int(get_current_timestamp())
        last_modify_ts = int(get_current_timestamp())
        note = XhsNote(
            user_id=content_item.get("user_id"),
            nickname=content_item.get("nickname"),
            avatar=content_item.get("avatar"),
            ip_location=content_item.get("ip_location"),
            add_ts=add_ts,
            last_modify_ts=last_modify_ts,
            note_id=content_item.get("note_id"),
            type=content_item.get("type"),
            title=content_item.get("title"),
            desc=content_item.get("desc"),
            video_url=content_item.get("video_url"),
            time=content_item.get("time"),
            last_update_time=content_item.get("last_update_time"),
            liked_count=_parse_count(content_item.get("liked_count")),
            collected_count=_parse_count(content_item.get("collected_count")),
            comment_count=_parse_count(content_item.get("comment_count")),
            share_count=_parse_count(content_item.get("share_count")),
            image_list=content_item.get("image_list", ""),
            tag_list=content_item.get("tag_list", ""),
            note_url=content_item.get("note_url"),
            source_keyword=content_item.get("source_keyword", ""),
            xsec_token=content_item.get("xsec_token", ""),
            raw_data=content_item.get("raw_data", ""),
            crawler_account=content_item.get("crawler_account") or get_current_account(),
        )
        session.add(note)

    async def update_content(self, session: AsyncSession, content_item: Dict):
        note_id = content_item.get("note_id")
        last_modify_ts = int(get_current_timestamp())
        update_data = {
            "last_modify_ts": last_modify_ts,
            "liked_count": _parse_count(content_item.get("liked_count")),
            "collected_count": _parse_count(content_item.get("collected_count")),
            "comment_count": _parse_count(content_item.get("comment_count")),
            "share_count": _parse_count(content_item.get("share_count")),
            "last_update_time": content_item.get("last_update_time"),
        }
        stmt = update(XhsNote).where(XhsNote.note_id == note_id).values(**update_data)
        await session.execute(stmt)

    async def content_is_exist(self, session: AsyncSession, note_id: str) -> bool:
        stmt = select(XhsNote).where(XhsNote.note_id == note_id)
        result = await session.execute(stmt)
        return result.first() is not None

    async def store_comment(self, comment_item: Dict):
        if not comment_item:
            return
        async with get_session() as session:
            comment_id = comment_item.get("comment_id")
            if not comment_id:
                return
            if await self.comment_is_exist(session, comment_id):
                await self.update_comment(session, comment_item)
            else:
                await self.add_comment(session, comment_item)
            await self.record_comment_observation(session, comment_item)

    async def record_comment_observation(
        self, session: AsyncSession, comment_item: Dict
    ) -> None:
        provenance = _runtime_provenance()
        run_id = provenance["run_id"]
        comment_id = str(comment_item.get("comment_id") or "").strip()
        note_id = str(comment_item.get("note_id") or "").strip()
        if not run_id or not comment_id or not note_id:
            return
        now_ts = int(get_current_timestamp())
        observation_kind = (
            "sub_comment"
            if str(comment_item.get("parent_comment_id") or "").strip()
            else "comment_page"
        )
        stmt = select(XhsCommentObservation).where(
            XhsCommentObservation.run_id == run_id,
            XhsCommentObservation.comment_id == comment_id,
            XhsCommentObservation.observation_kind == observation_kind,
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
        values = {
            "task_id": provenance["task_id"],
            "account_id": provenance["account_id"],
            "profile_id": provenance["profile_id"],
            "store_id": provenance["store_id"],
            "route_id": provenance["route_id"],
            "note_id": note_id,
            "parent_comment_id": str(comment_item.get("parent_comment_id") or ""),
            "observation_kind": observation_kind,
            "last_seen_ts": now_ts,
            "payload_hash": _payload_hash(comment_item),
            "legacy_source_label": str(
                comment_item.get("crawler_account") or provenance["account_id"]
            ),
        }
        if existing:
            await session.execute(
                update(XhsCommentObservation)
                .where(XhsCommentObservation.id == existing.id)
                .values(**values, seen_count=(existing.seen_count or 0) + 1)
            )
            return
        session.add(
            XhsCommentObservation(
                run_id=run_id,
                comment_id=comment_id,
                observed_at=now_ts,
                seen_count=1,
                **values,
            )
        )

    async def get_comment_ids_by_note_id(self, note_id: str) -> Set[str]:
        """Fetch all comment_ids for a given note_id."""
        async with get_session() as session:
            stmt = select(XhsNoteComment.comment_id).where(XhsNoteComment.note_id == note_id)
            result = await session.execute(stmt)
            return {row[0] for row in result.all() if row[0]}

    async def add_comment(self, session: AsyncSession, comment_item: Dict):
        add_ts = int(get_current_timestamp())
        last_modify_ts = int(get_current_timestamp())
        comment = XhsNoteComment(
            user_id=comment_item.get("user_id"),
            nickname=comment_item.get("nickname"),
            avatar=comment_item.get("avatar"),
            ip_location=comment_item.get("ip_location"),
            add_ts=add_ts,
            last_modify_ts=last_modify_ts,
            comment_id=comment_item.get("comment_id"),
            create_time=comment_item.get("create_time"),
            note_id=comment_item.get("note_id"),
            content=comment_item.get("content"),
            sub_comment_count=_parse_count(comment_item.get("sub_comment_count", 0)),
            pictures=comment_item.get("pictures", ""),
            parent_comment_id=str(comment_item.get("parent_comment_id", "")),
            like_count=_parse_count(comment_item.get("like_count")),
            raw_data=comment_item.get("raw_data", ""),
            crawler_account=comment_item.get("crawler_account") or get_current_account(),
        )
        session.add(comment)

    async def update_comment(self, session: AsyncSession, comment_item: Dict):
        comment_id = comment_item.get("comment_id")
        last_modify_ts = int(get_current_timestamp())
        update_data = {
            "last_modify_ts": last_modify_ts,
            "like_count": _parse_count(comment_item.get("like_count")),
            "sub_comment_count": _parse_count(comment_item.get("sub_comment_count", 0)),
        }
        stmt = update(XhsNoteComment).where(XhsNoteComment.comment_id == comment_id).values(**update_data)
        await session.execute(stmt)

    async def comment_is_exist(self, session: AsyncSession, comment_id: str) -> bool:
        stmt = select(XhsNoteComment).where(XhsNoteComment.comment_id == comment_id)
        result = await session.execute(stmt)
        return result.first() is not None

    async def store_creator(self, creator_item: Dict):
        user_id = creator_item.get("user_id")
        if not user_id:
            return
        async with get_session() as session:
            if await self.creator_is_exist(session, user_id):
                await self.update_creator(session, creator_item)
            else:
                await self.add_creator(session, creator_item)

    async def add_creator(self, session: AsyncSession, creator_item: Dict):
        add_ts = int(get_current_timestamp())
        last_modify_ts = int(get_current_timestamp())
        creator = XhsCreator(
            user_id=creator_item.get("user_id"),
            nickname=creator_item.get("nickname"),
            avatar=creator_item.get("avatar"),
            ip_location=creator_item.get("ip_location"),
            add_ts=add_ts,
            last_modify_ts=last_modify_ts,
            desc=creator_item.get("desc"),
            gender=creator_item.get("gender"),
            follows=int(creator_item.get("follows") or 0),
            fans=int(creator_item.get("fans") or 0),
            interaction=int(creator_item.get("interaction") or 0),
            tag_list=creator_item.get("tag_list", "{}"),
        )
        session.add(creator)

    async def update_creator(self, session: AsyncSession, creator_item: Dict):
        user_id = creator_item.get("user_id")
        last_modify_ts = int(get_current_timestamp())
        update_data = {
            "last_modify_ts": last_modify_ts,
            "nickname": creator_item.get("nickname"),
            "avatar": creator_item.get("avatar"),
            "desc": creator_item.get("desc"),
            "follows": int(creator_item.get("follows") or 0),
            "fans": int(creator_item.get("fans") or 0),
            "interaction": int(creator_item.get("interaction") or 0),
            "tag_list": creator_item.get("tag_list", "{}")
        }
        stmt = update(XhsCreator).where(XhsCreator.user_id == user_id).values(**update_data)
        await session.execute(stmt)

    async def creator_is_exist(self, session: AsyncSession, user_id: str) -> bool:
        stmt = select(XhsCreator).where(XhsCreator.user_id == user_id)
        result = await session.execute(stmt)
        return result.first() is not None

    async def get_all_content(self) -> List[Dict]:
        async with get_session() as session:
            stmt = select(XhsNote)
            result = await session.execute(stmt)
            return [item.__dict__ for item in result.scalars().all()]

    async def get_all_comments(self) -> List[Dict]:
        async with get_session() as session:
            stmt = select(XhsNoteComment)
            result = await session.execute(stmt)
            return [item.__dict__ for item in result.scalars().all()]

    async def get_note_count_by_keyword(self, keyword: str) -> int:
        """Get the count of notes with specific source keyword"""
        async with get_session() as session:
            stmt = select(func.count()).select_from(XhsNote).where(XhsNote.source_keyword == keyword)
            result = await session.execute(stmt)
            return result.scalar()

    async def get_all_note_ids_by_keyword(self, keyword: str) -> set:
        """Get all note IDs with specific source keyword"""
        async with get_session() as session:
            stmt = select(XhsNote.note_id).where(XhsNote.source_keyword == keyword)
            result = await session.execute(stmt)
            return {row[0] for row in result.all()}

    async def get_all_existing_ids(self) -> set:
        """Get ALL note IDs in database (for cross-keyword dedup)"""
        async with get_session() as session:
            stmt = select(XhsNote.note_id)
            result = await session.execute(stmt)
            return {row[0] for row in result.all()}


class XhsSqliteStoreImplement(XhsDbStoreImplement):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)


class XhsMongoStoreImplement(AbstractStore):
    """Xiaohongshu MongoDB storage implementation"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.mongo_store = MongoDBStoreBase(collection_prefix="xhs")

    async def store_content(self, content_item: Dict):
        """
        Store note content to MongoDB
        Args:
            content_item: Note content data
        """
        note_id = content_item.get("note_id")
        if not note_id:
            return

        await self.mongo_store.save_or_update(
            collection_suffix="contents",
            query={"note_id": note_id},
            data=content_item
        )
        utils.logger.info(f"[XhsMongoStoreImplement.store_content] Saved note {note_id} to MongoDB")

    async def store_comment(self, comment_item: Dict):
        """
        Store comment to MongoDB
        Args:
            comment_item: Comment data
        """
        comment_id = comment_item.get("comment_id")
        if not comment_id:
            return

        await self.mongo_store.save_or_update(
            collection_suffix="comments",
            query={"comment_id": comment_id},
            data=comment_item
        )
        utils.logger.info(f"[XhsMongoStoreImplement.store_comment] Saved comment {comment_id} to MongoDB")

    async def store_creator(self, creator_item: Dict):
        """
        Store creator information to MongoDB
        Args:
            creator_item: Creator data
        """
        user_id = creator_item.get("user_id")
        if not user_id:
            return

        await self.mongo_store.save_or_update(
            collection_suffix="creators",
            query={"user_id": user_id},
            data=creator_item
        )
        utils.logger.info(f"[XhsMongoStoreImplement.store_creator] Saved creator {user_id} to MongoDB")


class XhsExcelStoreImplement:
    """Xiaohongshu Excel storage implementation - Global singleton"""

    def __new__(cls, *args, **kwargs):
        from store.excel_store_base import ExcelStoreBase
        return ExcelStoreBase.get_instance(
            platform="xhs",
            crawler_type=crawler_type_var.get()
        )
