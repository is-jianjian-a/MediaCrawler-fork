# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tools/crawler_util.py
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
# @Time    : 2023/12/2 12:53
# @Desc    : Crawler utility functions

import base64
import json
import random
import re
import urllib
import urllib.parse
from io import BytesIO
from typing import Dict, List, Optional, Set, Tuple, cast

import httpx
from PIL import Image, ImageDraw, ImageShow
from playwright.async_api import BrowserContext, Cookie, Page

from . import utils
from .httpx_util import make_async_client


async def find_login_qrcode(page: Page, selector: str) -> str:
    """find login qrcode image from target selector"""
    try:
        elements = await page.wait_for_selector(
            selector=selector,
        )
        login_qrcode_img = str(await elements.get_property("src"))  # type: ignore
        if "http://" in login_qrcode_img or "https://" in login_qrcode_img:
            async with make_async_client(follow_redirects=True) as client:
                utils.logger.info(f"[find_login_qrcode] get qrcode by url:{login_qrcode_img}")
                resp = await client.get(login_qrcode_img, headers={"User-Agent": get_user_agent()})
                if resp.status_code == 200:
                    image_data = resp.content
                    base64_image = base64.b64encode(image_data).decode('utf-8')
                    return base64_image
                raise Exception(f"fetch login image url failed, response message:{resp.text}")
        return login_qrcode_img

    except Exception as e:
        print(e)
        return ""


async def find_qrcode_img_from_canvas(page: Page, canvas_selector: str) -> str:
    """
    find qrcode image from canvas element
    Args:
        page:
        canvas_selector:

    Returns:

    """

    # Wait for Canvas element to load
    canvas = await page.wait_for_selector(canvas_selector)

    # Take screenshot of Canvas element
    screenshot = await canvas.screenshot()

    # Convert screenshot to base64 format
    base64_image = base64.b64encode(screenshot).decode('utf-8')
    return base64_image


def show_qrcode(qr_code) -> None:  # type: ignore
    """parse base64 encode qrcode image and show it"""
    if "," in qr_code:
        qr_code = qr_code.split(",")[1]
    qr_code = base64.b64decode(qr_code)
    image = Image.open(BytesIO(qr_code))

    # Add a square border around the QR code and display it within the border to improve scanning accuracy.
    width, height = image.size
    new_image = Image.new('RGB', (width + 20, height + 20), color=(255, 255, 255))
    new_image.paste(image, (10, 10))
    draw = ImageDraw.Draw(new_image)
    draw.rectangle((0, 0, width + 19, height + 19), outline=(0, 0, 0), width=1)
    del ImageShow.UnixViewer.options["save_all"]
    new_image.show()


_CHROME_PROFILES = [
    {"version": "137", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="137", "Google Chrome";v="137", "Not/A)Brand";v="24"', "platform": '"Windows"'},
    {"version": "137", "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="137", "Google Chrome";v="137", "Not/A)Brand";v="24"', "platform": '"macOS"'},
    {"version": "136", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"', "platform": '"Windows"'},
    {"version": "136", "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"', "platform": '"macOS"'},
    {"version": "135", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36", "sec_ch_ua": '"Google Chrome";v="135", "Not-A.Brand";v="8", "Chromium";v="135"', "platform": '"Windows"'},
    {"version": "135", "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36", "sec_ch_ua": '"Google Chrome";v="135", "Not-A.Brand";v="8", "Chromium";v="135"', "platform": '"macOS"'},
    {"version": "134", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="134", "Google Chrome";v="134", "Not-A.Brand";v="8"', "platform": '"Windows"'},
    {"version": "134", "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="134", "Google Chrome";v="134", "Not-A.Brand";v="8"', "platform": '"macOS"'},
    {"version": "133", "ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="133", "Not(A:Brand";v="24", "Google Chrome";v="133"', "platform": '"Linux"'},
]

_VIEWPORT_PROFILES = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 720},
    {"width": 1680, "height": 1050},
    {"width": 2560, "height": 1440},
]


def get_chrome_profile() -> dict:
    return random.choice(_CHROME_PROFILES)


def get_random_viewport() -> dict:
    return random.choice(_VIEWPORT_PROFILES)


def get_user_agent() -> str:
    return random.choice(_CHROME_PROFILES)["ua"]


def get_mobile_user_agent() -> str:
    ua_list = [
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1"
    ]
    return random.choice(ua_list)


def convert_cookies(cookies: Optional[List[Cookie]]) -> Tuple[str, Dict]:
    if not cookies:
        return "", {}
    cookies_str = ";".join([f"{cookie.get('name')}={cookie.get('value')}" for cookie in cookies])
    cookie_dict = dict()
    for cookie in cookies:
        cookie_dict[cookie.get('name')] = cookie.get('value')
    return cookies_str, cookie_dict


async def convert_browser_context_cookies(
    browser_context: BrowserContext, urls: Optional[List[str]] = None
) -> Tuple[str, Dict]:
    cookies = (
        await browser_context.cookies(urls=urls)
        if urls
        else await browser_context.cookies()
    )
    return convert_cookies(cookies)


def convert_str_cookie_to_dict(cookie_str: str) -> Dict:
    cookie_dict: Dict[str, str] = dict()
    if not cookie_str:
        return cookie_dict
    for cookie in cookie_str.split(";"):
        cookie = cookie.strip()
        if not cookie:
            continue
        cookie_list = cookie.split("=")
        if len(cookie_list) != 2:
            continue
        cookie_value = cookie_list[1]
        if isinstance(cookie_value, list):
            cookie_value = "".join(cookie_value)
        cookie_dict[cookie_list[0]] = cookie_value
    return cookie_dict


def match_interact_info_count(count_str: str) -> int:
    if not count_str:
        return 0

    match = re.search(r'\d+', count_str)
    if match:
        number = match.group()
        return int(number)
    else:
        return 0


def format_proxy_info(ip_proxy_info) -> Tuple[Optional[Dict], Optional[str]]:
    """format proxy info for playwright and httpx"""
    # fix circular import issue
    from proxy.proxy_ip_pool import IpInfoModel
    ip_proxy_info = cast(IpInfoModel, ip_proxy_info)

    # Playwright proxy server should be in format "host:port" without protocol prefix
    server = f"{ip_proxy_info.ip}:{ip_proxy_info.port}"
    
    playwright_proxy = {
        "server": server,
    }
    
    # Only add username and password if they are not empty
    if ip_proxy_info.user and ip_proxy_info.password:
        playwright_proxy["username"] = ip_proxy_info.user
        playwright_proxy["password"] = ip_proxy_info.password
    
    # httpx 0.28.1 requires passing proxy URL string directly, not a dictionary
    if ip_proxy_info.user and ip_proxy_info.password:
        httpx_proxy = f"http://{ip_proxy_info.user}:{ip_proxy_info.password}@{ip_proxy_info.ip}:{ip_proxy_info.port}"
    else:
        httpx_proxy = f"http://{ip_proxy_info.ip}:{ip_proxy_info.port}"
    return playwright_proxy, httpx_proxy


def extract_text_from_html(html: str) -> str:
    """Extract text from HTML, removing all tags."""
    if not html:
        return ""

    # Remove script and style elements
    clean_html = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.DOTALL)
    # Remove all other tags
    clean_text = re.sub(r'<[^>]+>', '', clean_html).strip()
    return clean_text

def extract_url_params_to_dict(url: str) -> Dict:
    """Extract URL parameters to dict"""
    url_params_dict = dict()
    if not url:
        return url_params_dict
    parsed_url = urllib.parse.urlparse(url)
    url_params_dict = dict(urllib.parse.parse_qsl(parsed_url.query))
    return url_params_dict


def is_db_storage(db_type: str) -> bool:
    """Check if the storage type is database type"""
    return db_type in ["db", "sqlite", "postgres"]


async def check_and_adjust_crawler_count(store, keyword: str, max_count: int) -> Tuple[int, Set[str], Set[str]]:
    """
    Check database for existing notes and calculate how many new notes need to be crawled.
    
    Args:
        store: Store instance with get_note_count_by_keyword, get_all_note_ids_by_keyword and get_all_existing_ids methods
        keyword: Search keyword
        max_count: Maximum number of notes to crawl (from CRAWLER_MAX_NOTES_COUNT)
    
    Returns:
        Tuple of (adjusted_count, keyword_existing_ids, global_existing_ids)
        - adjusted_count: How many new notes need to be crawled (max_count - existing_count)
        - keyword_existing_ids: Set of note IDs that already exist in database for this keyword
        - global_existing_ids: Set of ALL note IDs in database (for cross-keyword dedup)
    """
    try:
        existing_count = await store.get_note_count_by_keyword(keyword)
        keyword_existing_ids = await store.get_all_note_ids_by_keyword(keyword)
        global_existing_ids = await store.get_all_existing_ids()
        
        if existing_count >= max_count:
            utils.logger.info(
                f"[check_and_adjust_crawler_count] Keyword '{keyword}': "
                f"already has {existing_count} notes, target is {max_count}, no new notes needed. "
                f"Global dedup pool size: {len(global_existing_ids)}"
            )
            return 0, keyword_existing_ids, global_existing_ids
        else:
            adjusted_count = max_count - existing_count
            utils.logger.info(
                f"[check_and_adjust_crawler_count] Keyword '{keyword}': "
                f"has {existing_count} existing notes, need to crawl {adjusted_count} new notes "
                f"(target: {max_count}). Global dedup pool size: {len(global_existing_ids)}"
            )
            return adjusted_count, keyword_existing_ids, global_existing_ids
            
    except AttributeError:
        utils.logger.warning(
            f"[check_and_adjust_crawler_count] Store does not support keyword-based queries. "
            f"Using default max_count: {max_count}"
        )
        return max_count, set(), set()
    except Exception as e:
        utils.logger.error(f"[check_and_adjust_crawler_count] Error checking database: {e}")
        return max_count, set(), set()

import asyncio
import random


async def smart_sleep() -> None:
    import config
    from tools import utils

    if config.ENABLE_RANDOM_SLEEP:
        base = random.uniform(config.CRAWLER_MIN_SLEEP_SEC, config.CRAWLER_MAX_SLEEP_SEC)
        jitter = random.gauss(0, base * 0.15)
        sleep_time = max(config.CRAWLER_MIN_SLEEP_SEC * 0.5, base + jitter)
        utils.logger.debug(f"[smart_sleep] Random sleep: {sleep_time:.2f}s (range: {config.CRAWLER_MIN_SLEEP_SEC}-{config.CRAWLER_MAX_SLEEP_SEC}s)")
    else:
        sleep_time = config.CRAWLER_MAX_SLEEP_SEC
        utils.logger.debug(f"[smart_sleep] Fixed sleep: {sleep_time:.2f}s")

    await asyncio.sleep(sleep_time)


def generate_html_report(items: list, platform: str, keyword: str, output_path: str = "test_report.html") -> None:
    """
    生成测试报告 HTML 文件，用于验证抓取结果
    
    Args:
        items: 抓取到的数据列表
        platform: 平台名称（中文）
        keyword: 搜索关键词
        output_path: 输出文件路径
    """
    from datetime import datetime
    
    # 限制展示数量
    import config
    max_items = min(len(items), config.TEST_REPORT_ITEM_COUNT)
    display_items = items[:max_items]
    
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # HTML 模板
    html_content = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>爬虫测试报告 - {platform} - {keyword}</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 40px 20px;
        }}
        .container {{
            max-width: 900px;
            margin: 0 auto;
            background: white;
            border-radius: 16px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
            overflow: hidden;
        }}
        .header {{
            background: linear-gradient(135deg, #ff6b6b 0%, #ee5a5a 100%);
            color: white;
            padding: 30px;
            text-align: center;
        }}
        .header h1 {{
            font-size: 28px;
            margin-bottom: 10px;
        }}
        .header p {{
            opacity: 0.9;
            font-size: 14px;
        }}
        .stats {{
            display: flex;
            justify-content: center;
            gap: 30px;
            margin-top: 15px;
            flex-wrap: wrap;
        }}
        .stat-item {{
            background: rgba(255, 255, 255, 0.2);
            padding: 10px 20px;
            border-radius: 20px;
            font-size: 14px;
        }}
        .content-list {{
            padding: 20px;
        }}
        .item-card {{
            background: #fafafa;
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 15px;
            border-left: 4px solid #ff6b6b;
            transition: all 0.3s ease;
        }}
        .item-card:hover {{
            transform: translateX(5px);
            box-shadow: 0 4px 15px rgba(0, 0, 0, 0.1);
        }}
        .item-title {{
            font-size: 18px;
            font-weight: 600;
            color: #333;
            margin-bottom: 10px;
            line-height: 1.5;
        }}
        .item-desc {{
            font-size: 15px;
            color: #666;
            line-height: 1.7;
            margin-bottom: 15px;
            display: -webkit-box;
            -webkit-line-clamp: 3;
            -webkit-box-orient: vertical;
            overflow: hidden;
        }}
        .item-meta {{
            display: flex;
            flex-wrap: wrap;
            gap: 15px;
            font-size: 13px;
            color: #888;
        }}
        .meta-item {{
            display: flex;
            align-items: center;
            gap: 5px;
        }}
        .meta-item svg {{
            width: 16px;
            height: 16px;
        }}
        .item-images {{
            display: flex;
            gap: 8px;
            margin-top: 15px;
            overflow-x: auto;
            padding-bottom: 10px;
        }}
        .item-images img {{
            width: 100px;
            height: 100px;
            object-fit: cover;
            border-radius: 8px;
            flex-shrink: 0;
        }}
        .item-link {{
            display: inline-block;
            margin-top: 15px;
            padding: 8px 16px;
            background: #667eea;
            color: white;
            text-decoration: none;
            border-radius: 20px;
            font-size: 13px;
            transition: background 0.3s;
        }}
        .item-link:hover {{
            background: #5a6fd6;
        }}
        .footer {{
            text-align: center;
            padding: 20px;
            color: #999;
            font-size: 13px;
            border-top: 1px solid #eee;
        }}
        .hot-badge {{
            display: inline-block;
            background: linear-gradient(135deg, #ff6b6b, #ee5a5a);
            color: white;
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 12px;
            margin-left: 10px;
        }}
        .video-badge {{
            display: inline-block;
            background: linear-gradient(135deg, #00d4ff, #0099cc);
            color: white;
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 12px;
            margin-left: 10px;
        }}
        .image-badge {{
            display: inline-block;
            background: linear-gradient(135deg, #7bed9f, #2ed573);
            color: white;
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 12px;
            margin-left: 10px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>爬虫测试报告</h1>
            <p>{platform} - 搜索关键词: {keyword}</p>
            <div class="stats">
                <div class="stat-item">📊 抓取总数: {len(items)}</div>
                <div class="stat-item">📝 展示数量: {max_items}</div>
                <div class="stat-item">🕐 生成时间: {current_time}</div>
            </div>
        </div>
        
        <div class="content-list">
    """
    
    # 生成内容列表
    for idx, item in enumerate(display_items, 1):
        # 获取数据
        title = item.get('title', item.get('desc', '')[:30] + '...')
        desc = item.get('desc', '')
        user_name = item.get('nickname', item.get('user_name', item.get('author_name', '未知')))
        liked_count = item.get('liked_count', item.get('like_count', '0'))
        collected_count = item.get('collected_count', item.get('collect_count', '0'))
        comment_count = item.get('comment_count', '0')
        share_count = item.get('share_count', '0')
        note_url = item.get('note_url', item.get('url', ''))
        image_list = item.get('image_list', item.get('images', []))
        if isinstance(image_list, str):
            try:
                import json
                image_list = json.loads(image_list)
            except:
                image_list = []
        item_type = item.get('type', 'normal')
        
        # 类型标签
        type_badge = ''
        if item_type == 'video':
            type_badge = '<span class="video-badge">视频</span>'
        elif item_type == 'normal' or 'image' in item_type.lower():
            type_badge = '<span class="image-badge">图文</span>'
        
        # 热度标签（超过一定数量显示热门）
        hot_badge = ''
        try:
            if int(liked_count) > 1000:
                hot_badge = '<span class="hot-badge">🔥 热门</span>'
        except:
            pass
        
        # 图片列表
        images_html = ''
        if image_list and isinstance(image_list, list):
            images_html = '<div class="item-images">'
            for img_url in image_list[:5]:  # 最多显示5张
                images_html += f'<img src="{img_url}" alt="图片" loading="lazy">'
            images_html += '</div>'
        
        # 链接
        link_html = ''
        if note_url:
            link_html = f'<a href="{note_url}" target="_blank" class="item-link">🔗 查看原文</a>'
        
        html_content += f"""
        <div class="item-card">
            <div class="item-title">{idx}. {title}{type_badge}{hot_badge}</div>
            <div class="item-desc">{desc}</div>
            <div class="item-meta">
                <div class="meta-item">👤 {user_name}</div>
                <div class="meta-item">❤️ {liked_count}</div>
                <div class="meta-item">⭐ {collected_count}</div>
                <div class="meta-item">💬 {comment_count}</div>
                <div class="meta-item">🔗 {share_count}</div>
            </div>
            {images_html}
            {link_html}
        </div>
        """
    
    # 结束部分
    html_content += """
        </div>
        
        <div class="footer">
            <p>📌 此报告用于验证爬虫抓取结果与手动搜索的一致性</p>
            <p style="margin-top: 5px; opacity: 0.7;">提示：可以直接复制标题到平台搜索验证</p>
        </div>
    </div>
</body>
</html>
    """
    
    # 写入文件
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    from tools import utils
    utils.logger.info(f"[generate_html_report] 测试报告已生成: {output_path}")
    print(f"\n🎉 测试报告已生成: {output_path}")
    print(f"📊 抓取数量: {len(items)}，展示数量: {max_items}")
    print(f"🌐 请用浏览器打开查看\n")

