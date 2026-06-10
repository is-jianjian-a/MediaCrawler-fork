# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/core.py
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

import asyncio
import os
import random
from asyncio import Task
from typing import Dict, List, Optional, Set

from playwright.async_api import (
    BrowserContext,
    BrowserType,
    Page,
    Playwright,
    async_playwright,
)
from tenacity import RetryError

import config
from base.base_crawler import AbstractCrawler
from model.m_xiaohongshu import NoteUrlInfo, CreatorUrlInfo
from proxy.proxy_ip_pool import IpInfoModel, create_ip_pool
from store import xhs as xhs_store
from tools import utils
from tools.crawler_util import check_and_adjust_crawler_count, is_db_storage, smart_sleep
from tools.cdp_browser import CDPBrowserManager
from var import crawler_type_var, source_keyword_var

from .client import XiaoHongShuClient
from .exception import DataFetchError, LoginError, NoteNotFoundError
from .field import SearchSortType, SearchNoteType
from .help import parse_note_info_from_note_url, parse_creator_info_from_url, get_search_id
from .login import XiaoHongShuLogin


class XiaoHongShuCrawler(AbstractCrawler):
    context_page: Page
    xhs_client: XiaoHongShuClient
    browser_context: BrowserContext
    cdp_manager: Optional[CDPBrowserManager]

    def __init__(self) -> None:
        self.index_url = "https://www.rednote.com" if config.XHS_INTERNATIONAL else "https://www.xiaohongshu.com"
        self.cookie_urls = [self.index_url]
        self.chrome_profile = utils.get_chrome_profile()
        self.user_agent = self.chrome_profile["ua"]
        self.cdp_manager = None
        self.ip_proxy_pool = None  # Proxy IP pool for automatic proxy refresh

    async def start(self) -> None:
        playwright_proxy_format, httpx_proxy_format = None, None
        if config.ENABLE_IP_PROXY:
            self.ip_proxy_pool = await create_ip_pool(config.IP_PROXY_POOL_COUNT, enable_validate_ip=True)
            ip_proxy_info: IpInfoModel = await self.ip_proxy_pool.get_proxy()
            playwright_proxy_format, httpx_proxy_format = utils.format_proxy_info(ip_proxy_info)

        async with async_playwright() as playwright:
            # Choose launch mode based on configuration
            if config.ENABLE_CDP_MODE:
                utils.logger.info("[XiaoHongShuCrawler] Launching browser using CDP mode")
                self.browser_context = await self.launch_browser_with_cdp(
                    playwright,
                    playwright_proxy_format,
                    self.user_agent,
                    headless=config.CDP_HEADLESS,
                )
            else:
                utils.logger.info("[XiaoHongShuCrawler] Launching browser using standard mode")
                # Launch a browser context.
                chromium = playwright.chromium
                self.browser_context = await self.launch_browser(
                    chromium,
                    playwright_proxy_format,
                    self.user_agent,
                    headless=config.HEADLESS,
                )
                # stealth.min.js is a js script to prevent the website from detecting the crawler.
                await self.browser_context.add_init_script(path="libs/stealth.min.js")

            self.context_page = await self.browser_context.new_page()
            await self.context_page.goto(self.index_url)

            # Create a client to interact with the Xiaohongshu website.
            self.xhs_client = await self.create_xhs_client(httpx_proxy_format)
            if not await self.xhs_client.pong():
                login_obj = XiaoHongShuLogin(
                    login_type=config.LOGIN_TYPE,
                    login_phone="",
                    browser_context=self.browser_context,
                    context_page=self.context_page,
                    cookie_str=config.COOKIES,
                )
                try:
                    await login_obj.begin()
                    await self.xhs_client.update_cookies(
                        browser_context=self.browser_context,
                        urls=self.cookie_urls,
                    )
                except LoginError as e:
                    utils.logger.error(f"[XiaoHongShuCrawler.start] Login failed: {e}")
                    return

            crawler_type_var.set(config.CRAWLER_TYPE)
            if config.CRAWLER_TYPE == "search":
                # Search for notes and retrieve their comment information.
                await self.search()
            elif config.CRAWLER_TYPE == "detail":
                # Get the information and comments of the specified post
                await self.get_specified_notes()
            elif config.CRAWLER_TYPE == "creator":
                # Get creator's information and their notes and comments
                await self.get_creators_and_notes()
            else:
                pass

            utils.logger.info("[XiaoHongShuCrawler.start] Xhs Crawler finished ...")

    async def _should_skip_keyword(self, keyword: str) -> tuple:
        """Check if keyword should be skipped due to smart crawler logic.

        Returns:
            Tuple of (adjusted_max_count, global_existing_ids)
            If adjusted_max_count <= 0, the keyword should be skipped.
        """
        if config.ENABLE_SMART_CRAWLER and is_db_storage(config.SAVE_DATA_OPTION) and not config.ENABLE_TEST_MODE:
            store = xhs_store.XhsStoreFactory.create_store()
            adjusted_max_count, _, global_existing_ids = await check_and_adjust_crawler_count(
                store, keyword, config.CRAWLER_MAX_NOTES_COUNT
            )
            if adjusted_max_count <= 0:
                utils.logger.info(f"[XiaoHongShuCrawler.search] Keyword '{keyword}' already fully crawled, skipping...")
                return 0, global_existing_ids
            return adjusted_max_count, global_existing_ids
        return config.CRAWLER_MAX_NOTES_COUNT, set()

    def _filter_search_items(self, items: list, existing_ids_set: Set[str]) -> list:
        """Filter out non-note items and already-existing notes from search results."""
        filtered_items = []
        for post_item in items:
            if post_item.get("model_type") in ("rec_query", "hot_query"):
                continue
            note_id = post_item.get("id")
            if note_id in existing_ids_set:
                utils.logger.debug(f"Skip existing note: {note_id}")
                continue
            filtered_items.append(post_item)
        return filtered_items

    async def _store_note_detail(self, note_detail: Dict, note_id: str,
                                  existing_ids_set: Set[str], test_mode_items: list,
                                  search_list_fallback: Optional[Dict] = None) -> tuple:
        data_to_store = note_detail
        is_fallback = False
        if not data_to_store and search_list_fallback:
            note_card = search_list_fallback.get("note_card") or search_list_fallback
            data_to_store = {
                "note_id": note_card.get("note_id", note_id),
                "type": note_card.get("type", ""),
                "title": note_card.get("display_title", ""),
                "desc": "",
                "user": note_card.get("user", {}),
                "interact_info": note_card.get("interact_info", {}),
                "image_list": note_card.get("image_list", []),
                "tag_list": note_card.get("tag_list", []),
                "time": note_card.get("time", 0),
                "last_update_time": note_card.get("last_update_time", 0),
                "ip_location": note_card.get("ip_location", ""),
                "xsec_token": search_list_fallback.get("xsec_token", ""),
                "xsec_source": search_list_fallback.get("xsec_source", ""),
                "_fallback": True,
            }
            is_fallback = True
            utils.logger.warning(f"[search] ⚠️ FALLBACK note_id={note_id} title=\"{note_card.get('display_title', '')[:40]}\" — detail API failed, using search list data (no desc/content)")

        if data_to_store:
            try:
                if config.ENABLE_TEST_MODE:
                    test_mode_items.append(data_to_store)
                else:
                    await xhs_store.update_xhs_note(data_to_store)
                    await self.get_notice_media(data_to_store)
                existing_ids_set.add(data_to_store.get("note_id", note_id))
                return True, existing_ids_set, 1, is_fallback
            except Exception as e:
                utils.logger.error(f"[XiaoHongShuCrawler.search] Failed to store note {data_to_store.get('note_id', note_id)}: {e}")
                existing_ids_set.add(note_id)
                return False, existing_ids_set, 0, False
        else:
            existing_ids_set.add(note_id)
            return False, existing_ids_set, 0, False

    def _log_search_summary(self, keyword: str, stop_reason: str,
                             total_attempted: int, failed_count: int,
                             actual_stored_count: int, pages_crawled: int,
                             api_search_count: int = 0, api_detail_count: int = 0, api_comment_count: int = 0,
                             fallback_count: int = 0):
        actual_new_count = total_attempted - failed_count
        api_total = api_search_count + api_detail_count + api_comment_count
        utils.logger.info("=" * 60)
        utils.logger.info(f"[XiaoHongShuCrawler.search] 📊 FINAL SUMMARY")
        utils.logger.info(f"  Keyword: {keyword}")
        utils.logger.info(f"  Target: {config.CRAWLER_MAX_NOTES_COUNT}")
        utils.logger.info(f"  Actual (this run): {actual_new_count}")
        utils.logger.info(f"  Actually stored: {actual_stored_count}")
        utils.logger.info(f"  Pages crawled: {pages_crawled}")
        utils.logger.info(f"  Total attempted: {total_attempted}")
        utils.logger.info(f"  Failed: {failed_count}")
        utils.logger.info(f"  Fallback (no desc/content): {fallback_count}")
        utils.logger.info(f"  API requests: search={api_search_count} detail={api_detail_count} comment={api_comment_count} total={api_total}")
        utils.logger.info(f"  ⛔ Stop reason: {stop_reason}")
        utils.logger.info("=" * 60)

        if actual_new_count < config.CRAWLER_MAX_NOTES_COUNT and stop_reason not in ["Reached target count"]:
            utils.logger.warning("[XiaoHongShuCrawler.search] ⚠️ Did not reach target count! Consider:")
            if "failure" in stop_reason.lower() or "error" in stop_reason.lower():
                utils.logger.warning("   - Check if your login is still valid")
                utils.logger.warning("   - Try reducing request frequency")
            if "page" in stop_reason.lower():
                utils.logger.warning("   - Check if this keyword has enough content")
                utils.logger.warning("   - Increase CRAWLER_MAX_EMPTY_PAGES in config")

    async def search(self) -> None:
        """Search for notes and retrieve their comment information."""
        utils.logger.info("[XiaoHongShuCrawler.search] Begin search Xiaohongshu keywords")
        xhs_limit_count = 20
        if config.CRAWLER_MAX_NOTES_COUNT < xhs_limit_count:
            config.CRAWLER_MAX_NOTES_COUNT = xhs_limit_count
        start_page = config.START_PAGE

        for keyword in config.KEYWORDS.split(","):
            if not keyword.strip():
                utils.logger.info("[XiaoHongShuCrawler.search] Skip empty keyword")
                continue

            source_keyword_var.set(keyword)
            utils.logger.info(f"[XiaoHongShuCrawler.search] Current search keyword: {keyword}")

            adjusted_max_count, existing_ids_set = await self._should_skip_keyword(keyword)
            if adjusted_max_count <= 0:
                continue

            test_mode_items = []
            actual_stored_count = 0
            page = 1
            search_id = get_search_id()
            max_pages = 50

            total_attempted = 0
            failed_count = 0
            consecutive_failures = 0
            consecutive_empty_pages = 0
            stop_reason = "Reached target count"
            api_search_count = 0
            api_detail_count = 0
            api_comment_count = 0
            fallback_count = 0
            prev_fallback_count = 0

            while adjusted_max_count > 0 and page <= max_pages:
                if page < start_page:
                    utils.logger.info(f"[XiaoHongShuCrawler.search] Skip page {page}")
                    page += 1
                    continue

                try:
                    utils.logger.info(f"[XiaoHongShuCrawler.search] search Xiaohongshu keyword: {keyword}, page: {page}")
                    current_sort = (SearchSortType(config.SORT_TYPE) if config.SORT_TYPE != "" else SearchSortType.GENERAL)
                    current_note_type = (SearchNoteType[config.NOTE_TYPE.upper()] if config.NOTE_TYPE != "" else SearchNoteType.ALL)
                    utils.logger.info(f"[XiaoHongShuCrawler.search] Config: SORT_TYPE={config.SORT_TYPE}, NOTE_TYPE={config.NOTE_TYPE} → API params: sort={current_sort.value}, note_type={current_note_type.value}")
                    notes_res = await self.xhs_client.get_note_by_keyword(
                        keyword=keyword,
                        search_id=search_id,
                        page=page,
                        sort=current_sort,
                        note_type=current_note_type,
                    )
                    api_search_count += 1

                    has_more = notes_res and notes_res.get("has_more", False)
                    semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)

                    filtered_items = self._filter_search_items(notes_res.get("items", {}), existing_ids_set)
                    utils.logger.info(f"[search] keyword=\"{keyword}\" page={page} | fetched={len(notes_res.get('items', []))} filtered={len(filtered_items)} (exist in DB) | storing {len(filtered_items)} notes")

                    if not filtered_items:
                        consecutive_empty_pages += 1
                        utils.logger.info(f"[XiaoHongShuCrawler.search] All notes on this page already exist (consecutive: {consecutive_empty_pages}), has_more: {has_more}")
                        if not has_more:
                            stop_reason = "No more content from API (has_more=false)"
                            utils.logger.info(f"[XiaoHongShuCrawler.search] API reports no more content, stopping.")
                            break
                        if consecutive_empty_pages >= config.CRAWLER_MAX_EMPTY_PAGES:
                            utils.logger.info(f"[XiaoHongShuCrawler.search] Reached max consecutive empty pages ({config.CRAWLER_MAX_EMPTY_PAGES}), but has_more={has_more}, continuing to next page...")
                        page += 1
                        continue

                    consecutive_empty_pages = 0

                    task_list = [
                        self.get_note_detail_async_task(
                            note_id=post_item.get("id"),
                            xsec_source=post_item.get("xsec_source"),
                            xsec_token=post_item.get("xsec_token"),
                            semaphore=semaphore,
                        ) for post_item in filtered_items
                    ]
                    note_details = await asyncio.gather(*task_list)
                    api_detail_count += len(task_list)

                    note_ids: List[str] = []
                    xsec_tokens: List[str] = []
                    prev_stored_count = actual_stored_count
                    prev_failed_count = failed_count
                    prev_fallback_count = fallback_count
                    for idx, note_detail in enumerate(note_details):
                        note_id = filtered_items[idx].get("id")
                        total_attempted += 1
                        success, existing_ids_set, stored_delta, is_fallback = await self._store_note_detail(
                            note_detail, note_id, existing_ids_set, test_mode_items,
                            search_list_fallback=filtered_items[idx]
                        )
                        if is_fallback:
                            fallback_count += 1
                        if success:
                            stored_note_id = note_detail.get("note_id") if note_detail else note_id
                            note_ids.append(stored_note_id)
                            xsec_tokens.append(filtered_items[idx].get("xsec_token", ""))
                            adjusted_max_count -= 1
                            actual_stored_count += stored_delta
                            consecutive_failures = 0
                        else:
                            failed_count += 1
                            consecutive_failures += 1

                    utils.logger.info(f"[search] keyword=\"{keyword}\" page={page} done | new={actual_stored_count - prev_stored_count} failed={failed_count - prev_failed_count} fallback={fallback_count - prev_fallback_count} | progress: {actual_stored_count}/{config.CRAWLER_MAX_NOTES_COUNT} | API: search={api_search_count} detail={api_detail_count} comment={api_comment_count}")

                    if total_attempted > 0:
                        failure_rate = failed_count / total_attempted
                        utils.logger.info(f"[XiaoHongShuCrawler.search] Failure stats - Total: {total_attempted}, Failed: {failed_count}, Rate: {failure_rate:.2%}, Consecutive failures: {consecutive_failures}")

                        if failure_rate > config.CRAWLER_MAX_FAILURE_RATE:
                            stop_reason = f"Failure rate too high ({failure_rate:.2%})"
                            utils.logger.error(f"[XiaoHongShuCrawler.search] Failure rate {failure_rate:.2%} exceeds threshold {config.CRAWLER_MAX_FAILURE_RATE:.0%}, stopping!")
                            break

                        if consecutive_failures >= config.CRAWLER_MAX_CONSECUTIVE_FAILURES:
                            stop_reason = f"Too many consecutive failures ({consecutive_failures})"
                            utils.logger.error(f"[XiaoHongShuCrawler.search] Consecutive failures {consecutive_failures} exceeds threshold {config.CRAWLER_MAX_CONSECUTIVE_FAILURES}, stopping!")
                            break

                    page += 1
                    await self.batch_get_note_comments(note_ids, xsec_tokens)
                    api_comment_count += len(note_ids)

                    await smart_sleep()
                    utils.logger.debug(f"Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds after page {page-1}")

                    if adjusted_max_count <= 0:
                        stop_reason = "Reached target count"
                        utils.logger.info(f"[XiaoHongShuCrawler.search] Reached target count: {config.CRAWLER_MAX_NOTES_COUNT}")
                        break

                    if not has_more:
                        stop_reason = "No more content from API (has_more=false)"
                        utils.logger.info(f"[XiaoHongShuCrawler.search] API reports no more content after page {page-1}, stopping.")
                        break

                    if page >= max_pages:
                        stop_reason = f"Reached max page limit ({max_pages})"
                        utils.logger.warning(f"[XiaoHongShuCrawler.search] Reached max page limit ({max_pages}), stopping early!")
                        break

                except DataFetchError:
                    stop_reason = "Login expired or API error"
                    utils.logger.error("[XiaoHongShuCrawler.search] Get note detail error (login expired or API error)")
                    break

            self._log_search_summary(keyword, stop_reason, total_attempted, failed_count, actual_stored_count, page - 1, api_search_count, api_detail_count, api_comment_count, fallback_count)

            if config.ENABLE_TEST_MODE and test_mode_items:
                utils.logger.info(f"[XiaoHongShuCrawler.search] 🧪 Test mode enabled, generating HTML report...")
                from tools.crawler_util import generate_html_report
                generate_html_report(
                    items=test_mode_items,
                    platform="小红书",
                    keyword=keyword,
                    output_path=config.TEST_REPORT_OUTPUT_PATH
                )

    async def get_creators_and_notes(self) -> None:
        """Get creator's notes and retrieve their comment information."""
        utils.logger.info("[XiaoHongShuCrawler.get_creators_and_notes] Begin get Xiaohongshu creators")
        for creator_url in config.XHS_CREATOR_ID_LIST:
            try:
                # Parse creator URL to get user_id and security tokens
                creator_info: CreatorUrlInfo = parse_creator_info_from_url(creator_url)
                utils.logger.info(f"[XiaoHongShuCrawler.get_creators_and_notes] Parse creator URL info: {creator_info}")
                user_id = creator_info.user_id

                # get creator detail info from web html content
                createor_info: Dict = await self.xhs_client.get_creator_info(
                    user_id=user_id,
                    xsec_token=creator_info.xsec_token,
                    xsec_source=creator_info.xsec_source
                )
                if createor_info:
                    await xhs_store.save_creator(user_id, creator=createor_info)
            except ValueError as e:
                utils.logger.error(f"[XiaoHongShuCrawler.get_creators_and_notes] Failed to parse creator URL: {e}")
                continue

            # Use fixed crawling interval
            crawl_interval = config.CRAWLER_MAX_SLEEP_SEC
            # Get all note information of the creator
            all_notes_list = await self.xhs_client.get_all_notes_by_creator(
                user_id=user_id,
                crawl_interval=crawl_interval,
                callback=self.fetch_creator_notes_detail,
                xsec_token=creator_info.xsec_token,
                xsec_source=creator_info.xsec_source,
            )

            note_ids = []
            xsec_tokens = []
            for note_item in all_notes_list:
                note_ids.append(note_item.get("note_id"))
                xsec_tokens.append(note_item.get("xsec_token"))
            await self.batch_get_note_comments(note_ids, xsec_tokens)

    async def fetch_creator_notes_detail(self, note_list: List[Dict]):
        """Concurrently obtain the specified post list and save the data"""
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list = [
            self.get_note_detail_async_task(
                note_id=post_item.get("note_id"),
                xsec_source=post_item.get("xsec_source"),
                xsec_token=post_item.get("xsec_token"),
                semaphore=semaphore,
            ) for post_item in note_list
        ]

        note_details = await asyncio.gather(*task_list)
        for note_detail in note_details:
            if note_detail:
                await xhs_store.update_xhs_note(note_detail)
                await self.get_notice_media(note_detail)

    async def get_specified_notes(self):
        """Get the information and comments of the specified post

        Note: Must specify note_id, xsec_source, xsec_token
        """
        get_note_detail_task_list = []
        for full_note_url in config.XHS_SPECIFIED_NOTE_URL_LIST:
            note_url_info: NoteUrlInfo = parse_note_info_from_note_url(full_note_url)
            utils.logger.info(f"[XiaoHongShuCrawler.get_specified_notes] Parse note url info: {note_url_info}")
            crawler_task = self.get_note_detail_async_task(
                note_id=note_url_info.note_id,
                xsec_source=note_url_info.xsec_source,
                xsec_token=note_url_info.xsec_token,
                semaphore=asyncio.Semaphore(config.MAX_CONCURRENCY_NUM),
            )
            get_note_detail_task_list.append(crawler_task)

        need_get_comment_note_ids = []
        xsec_tokens = []
        note_details = await asyncio.gather(*get_note_detail_task_list)
        for note_detail in note_details:
            if note_detail:
                need_get_comment_note_ids.append(note_detail.get("note_id", ""))
                xsec_tokens.append(note_detail.get("xsec_token", ""))
                await xhs_store.update_xhs_note(note_detail)
                await self.get_notice_media(note_detail)
        await self.batch_get_note_comments(need_get_comment_note_ids, xsec_tokens)

    async def get_note_detail_async_task(
        self,
        note_id: str,
        xsec_source: str,
        xsec_token: str,
        semaphore: asyncio.Semaphore,
    ) -> Optional[Dict]:
        """Get note detail

        Args:
            note_id:
            xsec_source:
            xsec_token:
            semaphore:

        Returns:
            Dict: note detail
        """
        note_detail = None
        utils.logger.debug(f"[get_note_detail_async_task] Begin get note detail, note_id: {note_id}")
        async with semaphore:
            try:
                try:
                    note_detail = await self.xhs_client.get_note_by_id(note_id, xsec_source, xsec_token)
                except RetryError:
                    pass

                if not note_detail:
                    note_detail = await self.xhs_client.get_note_by_id_from_html(note_id, xsec_source, xsec_token,
                                                                                 enable_cookie=True)
                    if not note_detail:
                        utils.logger.warning(f"[get_note_detail_async_task] Failed to get note detail, Id: {note_id}, skipping...")
                        return None

                note_detail.update({"xsec_token": xsec_token, "xsec_source": xsec_source})

                # Sleep after fetching note detail
                await smart_sleep()
                utils.logger.debug(f"Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds after fetching note {note_id}")

                return note_detail

            except NoteNotFoundError as ex:
                utils.logger.warning(f"[XiaoHongShuCrawler.get_note_detail_async_task] Note not found: {note_id}, {ex}")
                return None
            except DataFetchError as ex:
                utils.logger.error(f"[XiaoHongShuCrawler.get_note_detail_async_task] Get note detail error: {ex}")
                return None
            except KeyError as ex:
                utils.logger.error(f"[XiaoHongShuCrawler.get_note_detail_async_task] have not fund note detail note_id:{note_id}, err: {ex}")
                return None

    async def batch_get_note_comments(self, note_list: List[str], xsec_tokens: List[str]):
        """Batch get note comments"""
        if not config.ENABLE_GET_COMMENTS:
            utils.logger.debug(f"[XiaoHongShuCrawler.batch_get_note_comments] Crawling comment mode is not enabled")
            return

        utils.logger.debug(f"[XiaoHongShuCrawler.batch_get_note_comments] Begin batch get note comments, note list: {note_list}")
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list: List[Task] = []
        for index, note_id in enumerate(note_list):
            task = asyncio.create_task(
                self.get_comments(note_id=note_id, xsec_token=xsec_tokens[index], semaphore=semaphore),
                name=note_id,
            )
            task_list.append(task)
        await asyncio.gather(*task_list)

    async def get_comments(self, note_id: str, xsec_token: str, semaphore: asyncio.Semaphore):
        """Get note comments with keyword filtering and quantity limitation"""
        async with semaphore:
            utils.logger.info(f"[comments] fetching comments for note_id={note_id}")
            # Use fixed crawling interval
            crawl_interval = config.CRAWLER_MAX_SLEEP_SEC
            await self.xhs_client.get_note_all_comments(
                note_id=note_id,
                xsec_token=xsec_token,
                crawl_interval=crawl_interval,
                callback=xhs_store.batch_update_xhs_note_comments,
                max_count=config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES,
            )

            # Sleep after fetching comments
            await asyncio.sleep(crawl_interval)
            utils.logger.debug(f"Sleeping for {crawl_interval} seconds after fetching comments for note {note_id}")

    async def create_xhs_client(self, httpx_proxy: Optional[str]) -> XiaoHongShuClient:
        """Create Xiaohongshu client"""
        utils.logger.info("[XiaoHongShuCrawler.create_xhs_client] Begin create Xiaohongshu API client ...")
        cookie_str, cookie_dict = await utils.convert_browser_context_cookies(
            self.browser_context,
            urls=self.cookie_urls,
        )
        xhs_client_obj = XiaoHongShuClient(
            proxy=httpx_proxy,
            headers={
                "accept": "application/json, text/plain, */*",
                "accept-language": "zh-CN,zh;q=0.9",
                "cache-control": "no-cache",
                "content-type": "application/json;charset=UTF-8",
                "origin": self.index_url,
                "pragma": "no-cache",
                "priority": "u=1, i",
                "referer": f"{self.index_url}/",
                "sec-ch-ua": self.chrome_profile["sec_ch_ua"],
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": self.chrome_profile["platform"],
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-site",
                "user-agent": self.user_agent,
                "Cookie": cookie_str,
            },
            playwright_page=self.context_page,
            cookie_dict=cookie_dict,
            proxy_ip_pool=self.ip_proxy_pool,  # Pass proxy pool for automatic refresh
        )
        return xhs_client_obj

    async def launch_browser(
        self,
        chromium: BrowserType,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """Launch browser and create browser context"""
        utils.logger.info("[XiaoHongShuCrawler.launch_browser] Begin create browser context ...")
        viewport = utils.get_random_viewport()
        if config.SAVE_LOGIN_STATE:
            user_data_dir = os.path.join(os.getcwd(), "browser_data", config.USER_DATA_DIR % config.PLATFORM)  # type: ignore
            browser_context = await chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                accept_downloads=True,
                headless=headless,
                proxy=playwright_proxy,  # type: ignore
                viewport=viewport,
                user_agent=user_agent,
            )
            return browser_context
        else:
            browser = await chromium.launch(headless=headless, proxy=playwright_proxy)  # type: ignore
            browser_context = await browser.new_context(viewport=viewport, user_agent=user_agent)
            return browser_context

    async def launch_browser_with_cdp(
        self,
        playwright: Playwright,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """Launch browser using CDP mode"""
        try:
            self.cdp_manager = CDPBrowserManager()
            browser_context = await self.cdp_manager.launch_and_connect(
                playwright=playwright,
                playwright_proxy=playwright_proxy,
                user_agent=user_agent,
                headless=headless,
            )

            # Display browser information
            browser_info = await self.cdp_manager.get_browser_info()
            utils.logger.info(f"[XiaoHongShuCrawler] CDP browser info: {browser_info}")

            return browser_context

        except Exception as e:
            utils.logger.error(f"[XiaoHongShuCrawler] CDP mode launch failed, falling back to standard mode: {e}")
            # Fall back to standard mode
            chromium = playwright.chromium
            return await self.launch_browser(chromium, playwright_proxy, user_agent, headless)

    async def close(self):
        """Close browser context"""
        # Special handling if using CDP mode
        if self.cdp_manager:
            await self.cdp_manager.cleanup()
            self.cdp_manager = None
        else:
            await self.browser_context.close()
        utils.logger.info("[XiaoHongShuCrawler.close] Browser context closed ...")

    async def get_notice_media(self, note_detail: Dict):
        if not config.ENABLE_GET_MEIDAS:
            utils.logger.debug(f"[XiaoHongShuCrawler.get_notice_media] Crawling image mode is not enabled")
            return
        await self.get_note_images(note_detail)
        await self.get_notice_video(note_detail)

    async def get_note_images(self, note_item: Dict):
        """Get note images. Please use get_notice_media

        Args:
            note_item: Note item dictionary
        """
        if not config.ENABLE_GET_MEIDAS:
            return
        note_id = note_item.get("note_id")
        image_list: List[Dict] = note_item.get("image_list", [])

        xhs_store.normalize_image_list(image_list)

        if not image_list:
            return
        picNum = 0
        for pic in image_list:
            url = pic.get("url")
            if not url:
                continue
            content = await self.xhs_client.get_note_media(url)
            await asyncio.sleep(random.random())
            if content is None:
                continue
            extension_file_name = f"{picNum}.jpg"
            picNum += 1
            await xhs_store.update_xhs_note_image(note_id, content, extension_file_name)

    async def get_notice_video(self, note_item: Dict):
        """Get note videos. Please use get_notice_media

        Args:
            note_item: Note item dictionary
        """
        if not config.ENABLE_GET_MEIDAS:
            return
        note_id = note_item.get("note_id")

        videos = xhs_store.get_video_url_arr(note_item)

        if not videos:
            return
        videoNum = 0
        for url in videos:
            content = await self.xhs_client.get_note_media(url)
            await asyncio.sleep(random.random())
            if content is None:
                continue
            extension_file_name = f"{videoNum}.mp4"
            videoNum += 1
            await xhs_store.update_xhs_note_video(note_id, content, extension_file_name)
