# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/weibo/core.py
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

# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2023/12/23 15:41
# @Desc    : Weibo crawler main workflow code

import asyncio
import os
# import random  # Removed as we now use fixed config.CRAWLER_MAX_SLEEP_SEC intervals
from asyncio import Task
from typing import Dict, List, Optional, Set, Tuple

from playwright.async_api import (
    BrowserContext,
    BrowserType,
    Page,
    Playwright,
    async_playwright,
)

import config
from base.base_crawler import AbstractCrawler
from proxy.proxy_ip_pool import IpInfoModel, create_ip_pool
from store import weibo as weibo_store
from tools import utils
from tools.crawler_util import check_and_adjust_crawler_count, is_db_storage, smart_sleep
from tools.cdp_browser import CDPBrowserManager
from var import crawler_type_var, source_keyword_var

from .client import WeiboClient
from .exception import DataFetchError
from .field import SearchType
from .help import filter_search_result_card
from .login import WeiboLogin


class WeiboCrawler(AbstractCrawler):
    context_page: Page
    wb_client: WeiboClient
    browser_context: BrowserContext
    cdp_manager: Optional[CDPBrowserManager]

    def __init__(self):
        self.index_url = "https://www.weibo.com"
        self.mobile_index_url = "https://m.weibo.cn"
        self.cookie_urls = [self.mobile_index_url]
        self.user_agent = utils.get_user_agent()
        self.mobile_user_agent = utils.get_mobile_user_agent()
        self.cdp_manager = None
        self.ip_proxy_pool = None  # Proxy IP pool for automatic proxy refresh

    async def start(self):
        playwright_proxy_format, httpx_proxy_format = None, None
        if config.ENABLE_IP_PROXY:
            self.ip_proxy_pool = await create_ip_pool(config.IP_PROXY_POOL_COUNT, enable_validate_ip=True)
            ip_proxy_info: IpInfoModel = await self.ip_proxy_pool.get_proxy()
            playwright_proxy_format, httpx_proxy_format = utils.format_proxy_info(ip_proxy_info)

        async with async_playwright() as playwright:
            # Select launch mode based on configuration
            if config.ENABLE_CDP_MODE:
                utils.logger.info("[WeiboCrawler] Launching browser with CDP mode")
                self.browser_context = await self.launch_browser_with_cdp(
                    playwright,
                    playwright_proxy_format,
                    self.mobile_user_agent,
                    headless=config.CDP_HEADLESS,
                )
            else:
                utils.logger.info("[WeiboCrawler] Launching browser with standard mode")
                # Launch a browser context.
                chromium = playwright.chromium
                self.browser_context = await self.launch_browser(chromium, None, self.mobile_user_agent, headless=config.HEADLESS)

                # stealth.min.js is a js script to prevent the website from detecting the crawler.
                await self.browser_context.add_init_script(path="libs/stealth.min.js")


            self.context_page = await self.browser_context.new_page()
            await self.context_page.goto(self.index_url)
            await asyncio.sleep(2)


            # Create a client to interact with the xiaohongshu website.
            self.wb_client = await self.create_weibo_client(httpx_proxy_format)
            if not await self.wb_client.pong():
                login_obj = WeiboLogin(
                    login_type=config.LOGIN_TYPE,
                    login_phone="",  # your phone number
                    browser_context=self.browser_context,
                    context_page=self.context_page,
                    cookie_str=config.COOKIES,
                )
                await login_obj.begin()

                # After successful login, redirect to mobile website and update mobile cookies
                utils.logger.info("[WeiboCrawler.start] redirect weibo mobile homepage and update cookies on mobile platform")
                await self.context_page.goto(self.mobile_index_url)
                await asyncio.sleep(3)
                # Only get mobile cookies to avoid confusion between PC and mobile cookies
                await self.wb_client.update_cookies(
                    browser_context=self.browser_context,
                    urls=self.cookie_urls,
                )

            crawler_type_var.set(config.CRAWLER_TYPE)
            if config.CRAWLER_TYPE == "search":
                # Search for video and retrieve their comment information.
                await self.search()
            elif config.CRAWLER_TYPE == "detail":
                # Get the information and comments of the specified post
                await self.get_specified_notes()
            elif config.CRAWLER_TYPE == "creator":
                # Get creator's information and their notes and comments
                await self.get_creators_and_notes()
            else:
                pass
            utils.logger.info("[WeiboCrawler.start] Weibo Crawler finished ...")

    async def search(self):
        """
        search weibo note with keywords
        :return:
        """
        utils.logger.info("[WeiboCrawler.search] Begin search weibo keywords")
        weibo_limit_count = 10  # weibo limit page fixed value
        if config.CRAWLER_MAX_NOTES_COUNT < weibo_limit_count:
            config.CRAWLER_MAX_NOTES_COUNT = weibo_limit_count
        start_page = config.START_PAGE

        if config.WEIBO_SEARCH_TYPE == "default":
            search_type = SearchType.DEFAULT
        elif config.WEIBO_SEARCH_TYPE == "real_time":
            search_type = SearchType.REAL_TIME
        elif config.WEIBO_SEARCH_TYPE == "popular":
            search_type = SearchType.POPULAR
        elif config.WEIBO_SEARCH_TYPE == "video":
            search_type = SearchType.VIDEO
        else:
            utils.logger.error(f"[WeiboCrawler.search] Invalid WEIBO_SEARCH_TYPE: {config.WEIBO_SEARCH_TYPE}")
            return

        for keyword in config.KEYWORDS.split(","):
            if not keyword.strip():
                utils.logger.info("[WeiboCrawler.search] Skip empty keyword")
                continue
            
            adjusted_max_count = config.CRAWLER_MAX_NOTES_COUNT
            existing_ids_set: Set[int] = set()
            actual_stored_count = 0
            
            source_keyword_var.set(keyword)
            utils.logger.info(f"[search] keyword=\"{keyword}\" | starting search...")
            
            if config.ENABLE_SMART_CRAWLER and is_db_storage(config.SAVE_DATA_OPTION):
                store = weibo_store.WeiboStoreFactory.create_store()
                adjusted_max_count, _, existing_ids_set = await check_and_adjust_crawler_count(
                    store, keyword, config.CRAWLER_MAX_NOTES_COUNT
                )
                if adjusted_max_count <= 0:
                    utils.logger.info(f"[WeiboCrawler.search] Keyword '{keyword}' already fully crawled, skipping...")
                    continue
            else:
                existing_ids_set = set()
            
            page = 1
            max_pages = 50
            stop_reason = "Reached target count"
            api_search_count = 0
            api_detail_count = 0
            api_comment_count = 0
            total_attempted = 0
            failed_count = 0
            consecutive_failures = 0
            consecutive_empty_pages = 0
            
            while adjusted_max_count > 0 and page <= max_pages:
                if page < start_page:
                    utils.logger.info(f"[WeiboCrawler.search] Skip page: {page}")
                    page += 1
                    continue
                utils.logger.info(f"[WeiboCrawler.search] search weibo keyword: {keyword}, page: {page}")
                search_res = await self.wb_client.get_note_by_keyword(keyword=keyword, page=page, search_type=search_type)
                api_search_count += 1
                note_id_list: List[str] = []
                note_list = filter_search_result_card(search_res.get("cards"))
                note_list = await self.batch_get_notes_full_text(note_list)
                if not note_list:
                    stop_reason = "No more content from API (empty result)"
                    utils.logger.info(f"[WeiboCrawler.search] No notes from API for keyword '{keyword}' on page {page}, stopping.")
                    break
                
                consecutive_empty_pages = 0
                
                filtered_note_list = []
                for note_item in note_list:
                    if note_item:
                        mblog: Dict = note_item.get("mblog")
                        if mblog:
                            note_id = mblog.get("id")
                            if note_id in existing_ids_set:
                                utils.logger.debug(f"Skip existing note: {note_id}")
                                continue
                            filtered_note_list.append(note_item)
                
                if not filtered_note_list:
                    consecutive_empty_pages += 1
                    utils.logger.info(f"[WeiboCrawler.search] All notes on this page already exist (consecutive: {consecutive_empty_pages}), continuing to next page...")
                    page += 1
                    continue
                
                prev_stored = actual_stored_count
                prev_failed = failed_count
                for note_item in filtered_note_list:
                    if note_item:
                        mblog: Dict = note_item.get("mblog")
                        if mblog:
                            note_id = mblog.get("id")
                            note_id_list.append(note_id)
                            total_attempted += 1
                            try:
                                await weibo_store.update_weibo_note(note_item)
                                await self.get_note_images(mblog)
                                existing_ids_set.add(note_id)
                                adjusted_max_count -= 1
                                actual_stored_count += 1
                                consecutive_failures = 0
                            except Exception as e:
                                utils.logger.error(f"[WeiboCrawler.search] Failed to store note {note_id}: {e}")
                                failed_count += 1
                                consecutive_failures += 1
                utils.logger.info(f"[search] keyword=\"{keyword}\" page={page} done | new={actual_stored_count - prev_stored} failed={failed_count - prev_failed} | progress: {actual_stored_count}/{config.CRAWLER_MAX_NOTES_COUNT} | API: search={api_search_count} detail={api_detail_count} comment={api_comment_count}")
                
                if consecutive_failures >= config.CRAWLER_MAX_CONSECUTIVE_FAILURES:
                    stop_reason = f"Too many consecutive failures ({consecutive_failures})"
                    utils.logger.error(f"[WeiboCrawler.search] {stop_reason}, stopping!")
                    break
                
                if total_attempted > 0 and failed_count / total_attempted > config.CRAWLER_MAX_FAILURE_RATE:
                    stop_reason = f"Failure rate too high ({failed_count/total_attempted:.2%})"
                    utils.logger.error(f"[WeiboCrawler.search] {stop_reason}, stopping!")
                    break
                
                page += 1
                if adjusted_max_count <= 0:
                    stop_reason = "Reached target count"
                    utils.logger.info(f"[WeiboCrawler.search] Reached target count: {config.CRAWLER_MAX_NOTES_COUNT}")
                    break

                # Sleep after page navigation
                await smart_sleep()
                utils.logger.debug(f"Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds after page {page-1}")

                await self.batch_get_notes_comments(note_id_list)
                api_comment_count += len(note_id_list)
            
            actual_new_count = total_attempted - failed_count
            utils.logger.info("=" * 60)
            utils.logger.info(f"[WeiboCrawler.search] 📊 FINAL SUMMARY")
            utils.logger.info(f"  Keyword: {keyword}")
            utils.logger.info(f"  Target: {config.CRAWLER_MAX_NOTES_COUNT}")
            utils.logger.info(f"  Actual (this run): {actual_new_count}")
            utils.logger.info(f"  Actually stored: {actual_stored_count}")
            utils.logger.info(f"  Pages crawled: {page - 1}")
            utils.logger.info(f"  Total attempted: {total_attempted}")
            utils.logger.info(f"  Failed: {failed_count}")
            utils.logger.info(f"  API requests: search={api_search_count} detail={api_detail_count} comment={api_comment_count} total={api_search_count + api_detail_count + api_comment_count}")
            utils.logger.info(f"  ⛔ Stop reason: {stop_reason}")
            utils.logger.info("=" * 60)
            
            if actual_new_count < config.CRAWLER_MAX_NOTES_COUNT and stop_reason not in ["Reached target count"]:
                utils.logger.warning("[WeiboCrawler.search] ⚠️ Did not reach target count!")

    async def get_specified_notes(self):
        """
        get specified notes info
        :return:
        """
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list = [self.get_note_info_task(note_id=note_id, semaphore=semaphore) for note_id in config.WEIBO_SPECIFIED_ID_LIST]
        video_details = await asyncio.gather(*task_list)
        for note_item in video_details:
            if note_item:
                await weibo_store.update_weibo_note(note_item)
        await self.batch_get_notes_comments(config.WEIBO_SPECIFIED_ID_LIST)

    async def get_note_info_task(self, note_id: str, semaphore: asyncio.Semaphore) -> Optional[Dict]:
        """
        Get note detail task
        :param note_id:
        :param semaphore:
        :return:
        """
        async with semaphore:
            try:
                result = await self.wb_client.get_note_info_by_id(note_id)

                # Sleep after fetching note details
                await smart_sleep()
                utils.logger.debug(f"Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds after fetching note details {note_id}")

                return result
            except DataFetchError as ex:
                utils.logger.error(f"[WeiboCrawler.get_note_info_task] Get note detail error: {ex}")
                return None
            except KeyError as ex:
                utils.logger.error(f"[WeiboCrawler.get_note_info_task] have not fund note detail note_id:{note_id}, err: {ex}")
                return None

    async def batch_get_notes_comments(self, note_id_list: List[str]):
        """
        batch get notes comments
        :param note_id_list:
        :return:
        """
        if not config.ENABLE_GET_COMMENTS:
            utils.logger.debug(f"[WeiboCrawler.batch_get_note_comments] Crawling comment mode is not enabled")
            return

        utils.logger.info(f"[WeiboCrawler.batch_get_notes_comments] note ids:{note_id_list}")
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list: List[Task] = []
        for note_id in note_id_list:
            task = asyncio.create_task(self.get_note_comments(note_id, semaphore), name=note_id)
            task_list.append(task)
        await asyncio.gather(*task_list)

    async def get_note_comments(self, note_id: str, semaphore: asyncio.Semaphore):
        """
        get comment for note id
        :param note_id:
        :param semaphore:
        :return:
        """
        async with semaphore:
            try:
                utils.logger.info(f"[WeiboCrawler.get_note_comments] begin get note_id: {note_id} comments ...")

                # Sleep before fetching comments
                await smart_sleep()
                utils.logger.debug(f"Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds before fetching comments for note {note_id}")

                await self.wb_client.get_note_all_comments(
                    note_id=note_id,
                    crawl_interval=config.CRAWLER_MAX_SLEEP_SEC,  # Use fixed interval instead of random
                    callback=weibo_store.batch_update_weibo_note_comments,
                    max_count=config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES,
                )
            except DataFetchError as ex:
                utils.logger.error(f"[WeiboCrawler.get_note_comments] get note_id: {note_id} comment error: {ex}")
            except Exception as e:
                utils.logger.error(f"[WeiboCrawler.get_note_comments] may be been blocked, err:{e}")

    async def get_note_images(self, mblog: Dict):
        """
        get note images
        :param mblog:
        :return:
        """
        if not config.ENABLE_GET_MEIDAS:
            utils.logger.debug(f"[WeiboCrawler.get_note_images] Crawling image mode is not enabled")
            return

        pics: List = mblog.get("pics")
        if not pics:
            return
        for pic in pics:
            if isinstance(pic, str):
                url = pic
                pid = url.split("/")[-1].split(".")[0]
            elif isinstance(pic, dict):
                url = pic.get("url")
                pid = pic.get("pid", "")
            else:
                continue
            if not url:
                continue
            content = await self.wb_client.get_note_image(url)
            await smart_sleep()
            utils.logger.debug(f"Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds after fetching image")
            if content != None:
                extension_file_name = url.split(".")[-1]
                await weibo_store.update_weibo_note_image(pid, content, extension_file_name)

    async def get_creators_and_notes(self) -> None:
        """
        Get creator's information and their notes and comments
        Returns:

        """
        utils.logger.info("[WeiboCrawler.get_creators_and_notes] Begin get weibo creators")
        for user_id in config.WEIBO_CREATOR_ID_LIST:
            createor_info_res: Dict = await self.wb_client.get_creator_info_by_id(creator_id=user_id)
            if createor_info_res:
                createor_info: Dict = createor_info_res.get("userInfo", {})
                utils.logger.info(f"[WeiboCrawler.get_creators_and_notes] creator info: {createor_info}")
                if not createor_info:
                    raise DataFetchError("Get creator info error")
                await weibo_store.save_creator(user_id, user_info=createor_info)

                # Create a wrapper callback to get full text before saving data
                async def save_notes_with_full_text(note_list: List[Dict]):
                    # If full text fetching is enabled, batch get full text first
                    updated_note_list = await self.batch_get_notes_full_text(note_list)
                    await weibo_store.batch_update_weibo_notes(updated_note_list)

                # Get all note information of the creator
                all_notes_list = await self.wb_client.get_all_notes_by_creator_id(
                    creator_id=user_id,
                    container_id=f"107603{user_id}",
                    crawl_interval=0,
                    callback=save_notes_with_full_text,
                )

                note_ids = [note_item.get("mblog", {}).get("id") for note_item in all_notes_list if note_item.get("mblog", {}).get("id")]
                await self.batch_get_notes_comments(note_ids)

            else:
                utils.logger.error(f"[WeiboCrawler.get_creators_and_notes] get creator info error, creator_id:{user_id}")

    async def create_weibo_client(self, httpx_proxy: Optional[str]) -> WeiboClient:
        """Create xhs client"""
        utils.logger.info("[WeiboCrawler.create_weibo_client] Begin create weibo API client ...")
        cookie_str, cookie_dict = await utils.convert_browser_context_cookies(
            self.browser_context,
            urls=self.cookie_urls,
        )
        weibo_client_obj = WeiboClient(
            proxy=httpx_proxy,
            headers={
                "User-Agent": utils.get_mobile_user_agent(),
                "Cookie": cookie_str,
                "Origin": "https://m.weibo.cn",
                "Referer": "https://m.weibo.cn",
                "Content-Type": "application/json;charset=UTF-8",
            },
            playwright_page=self.context_page,
            cookie_dict=cookie_dict,
            proxy_ip_pool=self.ip_proxy_pool,  # Pass proxy pool for automatic refresh
        )
        return weibo_client_obj

    async def launch_browser(
        self,
        chromium: BrowserType,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """Launch browser and create browser context"""
        utils.logger.info("[WeiboCrawler.launch_browser] Begin create browser context ...")
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
                channel="chrome",  # Use system's Chrome stable version
            )
            return browser_context
        else:
            browser = await chromium.launch(headless=headless, proxy=playwright_proxy, channel="chrome")  # type: ignore
            browser_context = await browser.new_context(viewport=viewport, user_agent=user_agent)
            return browser_context

    async def launch_browser_with_cdp(
        self,
        playwright: Playwright,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """
        Launch browser with CDP mode
        """
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
            utils.logger.info(f"[WeiboCrawler] CDP browser info: {browser_info}")

            return browser_context

        except Exception as e:
            utils.logger.error(f"[WeiboCrawler] CDP mode startup failed, falling back to standard mode: {e}")
            # Fallback to standard mode
            chromium = playwright.chromium
            return await self.launch_browser(chromium, playwright_proxy, user_agent, headless)

    async def get_note_full_text(self, note_item: Dict) -> Dict:
        """
        Get full text content of a post
        If the post content is truncated (isLongText=True), request the detail API to get complete content
        :param note_item: Post data, contains mblog field
        :return: Updated post data
        """
        if not config.ENABLE_WEIBO_FULL_TEXT:
            return note_item

        mblog = note_item.get("mblog", {})
        if not mblog:
            return note_item

        # Check if it's a long text
        is_long_text = mblog.get("isLongText", False)
        if not is_long_text:
            return note_item

        note_id = mblog.get("id")
        if not note_id:
            return note_item

        try:
            utils.logger.info(f"[WeiboCrawler.get_note_full_text] Fetching full text for note: {note_id}")
            full_note = await self.wb_client.get_note_info_by_id(note_id)
            if full_note and full_note.get("mblog"):
                # Replace original content with complete content
                note_item["mblog"] = full_note["mblog"]
                utils.logger.info(f"[WeiboCrawler.get_note_full_text] Successfully fetched full text for note: {note_id}")

            # Sleep after request to avoid rate limiting
            await smart_sleep()
        except DataFetchError as ex:
            utils.logger.error(f"[WeiboCrawler.get_note_full_text] Failed to fetch full text for note {note_id}: {ex}")
        except Exception as ex:
            utils.logger.error(f"[WeiboCrawler.get_note_full_text] Unexpected error for note {note_id}: {ex}")

        return note_item

    async def batch_get_notes_full_text(self, note_list: List[Dict]) -> List[Dict]:
        """
        Batch get full text content of posts
        :param note_list: List of posts
        :return: Updated list of posts
        """
        if not config.ENABLE_WEIBO_FULL_TEXT:
            return note_list

        result = []
        for note_item in note_list:
            updated_note = await self.get_note_full_text(note_item)
            result.append(updated_note)
        return result

    async def close(self):
        """Close browser context"""
        # Special handling if using CDP mode
        if self.cdp_manager:
            await self.cdp_manager.cleanup()
            self.cdp_manager = None
        else:
            await self.browser_context.close()
        utils.logger.info("[WeiboCrawler.close] Browser context closed ...")
