# -*- coding: utf-8 -*-
import os
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/config/base_config.py
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

# Basic configuration
PLATFORM = "xhs"  # Platform, xhs | dy | ks | bili | wb | tieba | zhihu

# 是否使用海外版小红书 (rednote.com)
# 开启后 API 走 webapi.rednote.com，cookie 域使用 .rednote.com
XHS_INTERNATIONAL = False

KEYWORDS = os.getenv("MEDIACRAWLER_KEYWORDS", "华为和苹果卡顿对比,华为和苹果性能对比,华为和苹果流畅度对比,华为和苹果稳定性对比,华为和苹果丝滑对比,华为稳定,华为丝滑,苹果丝滑,华为性能,苹果稳定,华为卡顿,苹果性能,苹果流畅,苹果卡顿,华为流畅")  # Keyword search configuration, separated by English commas
LOGIN_TYPE = "qrcode"  # qrcode or phone or cookie
COOKIES = ""
CRAWLER_TYPE = (
    "search"  # Crawling type, search (keyword search) | detail (post details) | creator (creator homepage data)
)

# ==================== Xiaohongshu search controls ====================
# Sorting method, the specific enumeration value is in media_platform/xhs/field.py
# general = 默认/综合排序, popularity_descending = 最热排序, time_descending = 最新排序
SORT_TYPE = os.getenv("MEDIACRAWLER_XHS_SORT_TYPE", "time_descending")

# Note type filter, the specific enumeration value is in media_platform/xhs/field.py
# all = 全部(图文+视频), video = 仅视频, image = 仅图文
NOTE_TYPE = os.getenv("MEDIACRAWLER_XHS_NOTE_TYPE", "image")

# 只入库该日期之后发布的小红书笔记。为空表示不过滤。
# 支持格式：YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS；按本地时区解释。
XHS_NOTE_PUBLISH_DATE_AFTER = os.getenv("MEDIACRAWLER_XHS_NOTE_PUBLISH_DATE_AFTER", "2026-06-10")

# 最新排序从新到旧抓取时，遇到早于 XHS_NOTE_PUBLISH_DATE_AFTER 的笔记后停止当前关键词。
# 适用于“抓到某日期为止”的任务；关闭时只过滤入库，不提前停止。
XHS_STOP_WHEN_BEFORE_DATE = os.getenv("MEDIACRAWLER_XHS_STOP_WHEN_BEFORE_DATE", "false").lower() in ("1", "true", "yes")

# 从新到旧搜索时的“搜索到的帖子数”安全上限。
# 0 表示不按数量限制，继续翻页直到日期下限、has_more=false 或其他异常停止条件。
XHS_SEARCH_MAX_ITEMS = int(os.getenv("MEDIACRAWLER_XHS_SEARCH_MAX_ITEMS", "0"))

# 控制爬取的帖子/视频数量
CRAWLER_MAX_NOTES_COUNT = int(os.getenv("MEDIACRAWLER_CRAWLER_MAX_NOTES_COUNT", "20"))
# Whether to enable comment crawling mode. Comment crawling is enabled by default.
ENABLE_GET_COMMENTS = os.getenv("MEDIACRAWLER_ENABLE_GET_COMMENTS", "true").lower() in ("1", "true", "yes")
# Control the number of crawled first-level comments (single video/post)
CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES = int(os.getenv("MEDIACRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES", "10"))
# Maximum total second-level comments scanned per post. This is independent
# from the first-level limit above.
CRAWLER_MAX_SUB_COMMENTS_COUNT_SINGLENOTES = int(os.getenv("MEDIACRAWLER_MAX_SUB_COMMENTS_COUNT_SINGLENOTES", "10"))
# Whether to enable the mode of crawling second-level comments. By default, crawling of second-level comments is not enabled.
# If the old version of the project uses db, you need to refer to schema/tables.sql line 287 to add table fields.
ENABLE_GET_SUB_COMMENTS = os.getenv("MEDIACRAWLER_ENABLE_GET_SUB_COMMENTS", "true").lower() in ("1", "true", "yes")

# Crawl interval
# 是否启用随机睡眠间隔
ENABLE_RANDOM_SLEEP = os.getenv("MEDIACRAWLER_ENABLE_RANDOM_SLEEP", "true").lower() in ("1", "true", "yes")
# 随机睡眠的最小时间（秒）
CRAWLER_MIN_SLEEP_SEC = int(os.getenv("MEDIACRAWLER_CRAWLER_MIN_SLEEP_SEC", "20"))
# 随机睡眠的最大时间（秒）
CRAWLER_MAX_SLEEP_SEC = int(os.getenv("MEDIACRAWLER_CRAWLER_MAX_SLEEP_SEC", "40"))
# 评论抓取间隔（秒）。评论接口本身较轻，但仍需要避免连续请求。
CRAWLER_COMMENT_SLEEP_SEC = int(os.getenv("MEDIACRAWLER_CRAWLER_COMMENT_SLEEP_SEC", "5"))
# 单条帖子详情请求超时（秒）。超过后跳过该条，避免任务静默卡死。
XHS_NOTE_DETAIL_TIMEOUT_SEC = int(os.getenv("MEDIACRAWLER_XHS_NOTE_DETAIL_TIMEOUT_SEC", "75"))

# Whether to enable IP proxy
ENABLE_IP_PROXY = False
# Number of proxy IP pools
IP_PROXY_POOL_COUNT = 2
# Proxy IP provider name
IP_PROXY_PROVIDER_NAME = "kuaidaili"  # kuaidaili | wandouhttp

# Setting to True will not open the browser (headless browser)
# Setting False will open a browser
# If Xiaohongshu keeps scanning the code to log in but fails, open the browser and manually pass the sliding verification code.
# If Douyin keeps prompting failure, open the browser and see if mobile phone number verification appears after scanning the QR code to log in. If it does, manually go through it and try again.
HEADLESS = False

# Whether to save login status
SAVE_LOGIN_STATE = True

# ==================== CDP (Chrome DevTools Protocol) 配置 ====================
# 是否启用 CDP 模式 - 使用用户本地的 Chrome/Edge 浏览器进行爬取，具有更好的反检测能力
# 开启后，会自动检测并启动用户的 Chrome/Edge 浏览器，通过 CDP 协议进行控制
# 该方式使用真实浏览器环境，包括用户的扩展、Cookie 和设置，大幅降低被风控检测的风险
ENABLE_CDP_MODE = os.getenv("MEDIACRAWLER_ENABLE_CDP", "false").lower() in ("1", "true", "yes")

# CDP 调试端口，用于与浏览器通信
# 如果端口被占用，系统会自动尝试下一个可用端口
CDP_DEBUG_PORT = int(os.getenv("MEDIACRAWLER_CDP_DEBUG_PORT", "9222"))

# 是否强制要求 CDP 模式可用。开启后 CDP 连接失败不会自动回退标准浏览器模式。
REQUIRE_CDP_MODE = os.getenv("MEDIACRAWLER_REQUIRE_CDP", "false").lower() in ("1", "true", "yes")

# 自定义浏览器路径（可选）
# 如果为空，系统会自动检测 Chrome/Edge 的安装路径
# Windows 示例: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
# macOS 示例: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CUSTOM_BROWSER_PATH = os.getenv("MEDIACRAWLER_BROWSER_PATH", "")

# 是否在 CDP 模式下启用无头模式
# 注意：即使设置为 True，某些反检测功能在无头模式下可能无法正常工作
CDP_HEADLESS = False

# 浏览器启动超时时间（秒）
BROWSER_LAUNCH_TIMEOUT = 60

# 是否连接用户已打开的浏览器，而不是启动新的浏览器
# 开启后，程序会连接一个已经启用了远程调试的浏览器
# 用户需要在 Chrome 中开启远程调试：chrome://inspect/#remote-debugging
# 或者使用命令行参数启动 Chrome：--remote-debugging-port=9222
# 这种方式反检测效果最好，因为直接使用用户真实浏览器的所有 Cookie、扩展和浏览历史
CDP_CONNECT_EXISTING = os.getenv("MEDIACRAWLER_CDP_CONNECT_EXISTING", "true").lower() in ("1", "true", "yes")

# 程序结束时是否自动关闭浏览器
# 设置为 False 可以保持浏览器运行，方便调试
AUTO_CLOSE_BROWSER = True

# Data saving type option configuration, supports: csv, db, json, jsonl, sqlite, excel, postgres. It is best to save to DB, with deduplication function.
SAVE_DATA_OPTION = "sqlite"  # csv or db or json or jsonl or sqlite or excel or postgres

# Data saving path, if not specified by default, it will be saved to the data folder.
SAVE_DATA_PATH = ""

# Browser file configuration cached by the user's browser
# 旧账号浏览器数据目录（已备份）
# USER_DATA_DIR = "%s_user_data_dir"

# 浏览器数据目录（可通过 --account 参数切换）
USER_DATA_DIR = os.getenv(
    "MEDIACRAWLER_USER_DATA_DIR", "%s_user_data_dir"
)  # %s will be replaced by platform name

# 账号 → 浏览器数据目录映射
# _ACCOUNT_USER_DATA_MAP = {
#     "02": "%s_user_data_dir_account02",
#     "03": "%s_user_data_dir_account03",
# }

# The number of pages to start crawling starts from the first page by default
START_PAGE = 1

# 是否启用智能增量抓取（先检查数据库已有数量）
# 仅在 SAVE_DATA_OPTION 为 db/sqlite/postgres 时生效
ENABLE_SMART_CRAWLER = True

# 智能抓取数量模式：
# total = CRAWLER_MAX_NOTES_COUNT 表示每个关键词库内目标总量，已有数量达到后跳过
# incremental = CRAWLER_MAX_NOTES_COUNT 表示每次运行在已有基础上新增的数量
SMART_CRAWLER_COUNT_MODE = os.getenv("MEDIACRAWLER_SMART_CRAWLER_COUNT_MODE", "incremental")

# 控制并发爬虫数量
MAX_CONCURRENCY_NUM = int(os.getenv("MEDIACRAWLER_MAX_CONCURRENCY_NUM", "1"))

# Whether to enable crawling media mode (including image or video resources), crawling media is not enabled by default
ENABLE_GET_MEDIAS = False

# 测试模式配置
ENABLE_TEST_MODE = False  # 开启后不入库，只生成 HTML 报告用于验证
TEST_REPORT_OUTPUT_PATH = "test_report.html"  # HTML 报告输出路径
TEST_REPORT_ITEM_COUNT = 20  # 报告中展示的最大条目数量

# word cloud related
# Whether to enable generating comment word clouds
ENABLE_GET_WORDCLOUD = False
# Custom words and their groups
# Add rule: xx:yy where xx is a custom-added phrase, and yy is the group name to which the phrase xx is assigned.
CUSTOM_WORDS = {
    "零几": "年份",  # Recognize "zero points" as a whole
    "高频词": "专业术语",  # Example custom words
}

# Deactivate (disabled) word file path
STOP_WORDS_FILE = "./docs/hit_stopwords.txt"

# Chinese font file path
FONT_PATH = "./docs/STZHONGS.TTF"


# 允许的最大失败比例（0-1），超过此比例程序中断
CRAWLER_MAX_FAILURE_RATE = 0.3
# 连续失败的最大数量，超过此数量程序中断
CRAWLER_MAX_CONSECUTIVE_FAILURES = 5
# 连续空页的最大数量，超过此数量认为没有更多内容
CRAWLER_MAX_EMPTY_PAGES = 3

# 是否禁用 SSL 证书验证。仅在使用企业代理、Burp Suite、mitmproxy 等会注入自签名证书的中间人代理时设为 True。
# 警告：禁用 SSL 验证将使所有流量暴露于中间人攻击风险，请勿在生产环境中开启。
DISABLE_SSL_VERIFY = False

from .bilibili_config import *
from .xhs_config import *
from .dy_config import *
from .ks_config import *
from .weibo_config import *
from .tieba_config import *
from .zhihu_config import *
