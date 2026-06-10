# MediaCrawler Code Wiki

## 1. 项目概述

MediaCrawler 是一个基于 Python 的社交媒体爬虫项目，支持小红书、抖音、快手、B站、微博、百度贴吧、知乎等 7 个主流平台的数据采集。项目采用 Playwright + CDP 浏览器自动化技术，通过模拟真实用户行为绕过反爬检测，支持关键词搜索、帖子详情、创作者主页三种爬取模式，并提供多种数据存储方式。

- **作者**: relakkes@gmail.com (程序员阿江)
- **协议**: NON-COMMERCIAL LEARNING LICENSE 1.1（仅供学习研究，禁止商业用途）
- **Python 版本**: >= 3.11

---

## 2. 项目架构

### 2.1 整体架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                        入口层 (Entry)                            │
│  main.py (CLI)  │  api/main.py (WebUI API)  │  cmd_arg/arg.py  │
└──────────┬──────────────────┬──────────────────────┬────────────┘
           │                  │                      │
           ▼                  ▼                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                     爬虫工厂 (CrawlerFactory)                     │
│  根据 PLATFORM 配置创建对应平台的 Crawler 实例                      │
└──────────┬─────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────┐
│                   平台爬虫层 (media_platform/)                    │
│  xhs/ │ douyin/ │ kuaishou/ │ bilibili/ │ weibo/ │ tieba/ │ zhihu/ │
│  每个平台包含: core.py, client.py, login.py, field.py, help.py   │
└────┬──────────┬──────────┬──────────────────────────────────────┘
     │          │          │
     ▼          ▼          ▼
┌─────────┐ ┌─────────┐ ┌──────────┐
│ 浏览器层 │ │ 代理层  │ │ 存储层   │
│ Playwright│ │ proxy/  │ │ store/   │
│ CDP模式  │ │ IP池    │ │ 工厂模式 │
└─────────┘ └─────────┘ └────┬─────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
        ┌──────────┐   ┌──────────┐   ┌──────────┐
        │ 数据库层  │   │ 文件层   │   │ 缓存层   │
        │ database/ │   │ JSON/CSV │   │ cache/   │
        │ SQLAlchemy│   │ Excel    │   │ Redis    │
        └──────────┘   └──────────┘   └──────────┘
```

### 2.2 目录结构

```
MediaCrawler/
├── main.py                    # CLI 入口，CrawlerFactory + 生命周期管理
├── var.py                     # 全局 ContextVar 变量
├── base/                      # 抽象基类定义
│   └── base_crawler.py        # AbstractCrawler, AbstractLogin, AbstractStore, AbstractApiClient
├── config/                    # 配置模块
│   ├── base_config.py         # 基础配置（平台、关键词、爬取数量、CDP等）
│   ├── db_config.py           # 数据库连接配置（MySQL/Redis/SQLite/MongoDB/PostgreSQL）
│   ├── xhs_config.py          # 小红书专属配置
│   ├── dy_config.py           # 抖音专属配置
│   ├── ks_config.py           # 快手专属配置
│   ├── bilibili_config.py     # B站专属配置
│   ├── weibo_config.py        # 微博专属配置
│   ├── tieba_config.py        # 贴吧专属配置
│   └── zhihu_config.py        # 知乎专属配置
├── media_platform/            # 各平台爬虫实现
│   ├── xhs/                   # 小红书
│   ├── douyin/                # 抖音
│   ├── kuaishou/              # 快手
│   ├── bilibili/              # B站
│   ├── weibo/                 # 微博
│   ├── tieba/                 # 百度贴吧
│   └── zhihu/                 # 知乎
├── store/                     # 数据存储层
│   ├── xhs/                   # 小红书存储实现
│   ├── douyin/                # 抖音存储实现
│   ├── kuaishou/              # 快手存储实现
│   ├── bilibili/              # B站存储实现
│   ├── weibo/                 # 微博存储实现
│   ├── tieba/                 # 贴吧存储实现
│   ├── zhihu/                 # 知乎存储实现
│   └── excel_store_base.py    # Excel 存储基类
├── database/                  # 数据库层
│   ├── db.py                  # 数据库初始化入口
│   ├── db_session.py          # SQLAlchemy 会话管理
│   ├── models.py              # ORM 模型定义
│   └── mongodb_store_base.py  # MongoDB 存储基类
├── model/                     # Pydantic 数据模型
│   ├── m_xiaohongshu.py       # 小红书 URL 解析模型
│   ├── m_douyin.py            # 抖音 URL 解析模型
│   ├── m_kuaishou.py          # 快手 URL 解析模型
│   ├── m_bilibili.py          # B站 URL 解析模型
│   ├── m_weibo.py             # 微博 URL 解析模型
│   ├── m_baidu_tieba.py       # 贴吧数据模型
│   └── m_zhihu.py             # 知乎数据模型
├── cache/                     # 缓存层
│   ├── abs_cache.py           # 缓存抽象基类
│   ├── cache_factory.py       # 缓存工厂
│   ├── local_cache.py         # 本地内存缓存
│   └── redis_cache.py         # Redis 缓存
├── proxy/                     # 代理 IP 层
│   ├── base_proxy.py          # 代理提供者抽象基类
│   ├── proxy_ip_pool.py       # IP 代理池
│   ├── proxy_mixin.py         # 代理混入
│   ├── types.py               # 代理类型定义
│   └── providers/             # 代理服务商实现
│       ├── kuaidl_proxy.py    # 快代理
│       ├── wandou_http_proxy.py # 蜿豆HTTP
│       └── jishu_http_proxy.py  # 极速HTTP
├── tools/                     # 工具模块
│   ├── utils.py               # 通用工具函数
│   ├── crawler_util.py        # 爬虫工具（智能睡眠、计数检查、二维码等）
│   ├── cdp_browser.py         # CDP 浏览器管理器
│   ├── browser_launcher.py    # 浏览器启动器
│   ├── app_runner.py          # 应用运行器（信号处理、优雅退出）
│   ├── async_file_writer.py   # 异步文件写入器
│   ├── httpx_util.py          # httpx 客户端工具
│   ├── time_util.py           # 时间工具
│   ├── slider_util.py         # 滑块验证工具
│   ├── easing.py              # 缓动函数
│   ├── words.py               # 词云生成
│   └── file_header_manager.py # 文件头管理
├── constant/                  # 常量定义
│   ├── baidu_tieba.py         # 贴吧常量
│   └── zhihu.py               # 知乎常量
├── api/                       # WebUI API 层
│   ├── main.py                # FastAPI 应用
│   ├── routers/               # API 路由
│   │   ├── crawler.py         # 爬虫控制路由
│   │   ├── data.py            # 数据查询路由
│   │   └── websocket.py       # WebSocket 实时日志
│   ├── schemas/               # Pydantic 请求/响应模型
│   ├── services/              # 业务逻辑层
│   │   └── crawler_manager.py # 爬虫进程管理器
│   └── webui/                 # 前端静态文件
├── libs/                      # JS 脚本
│   ├── stealth.min.js         # 反检测脚本
│   ├── douyin.js              # 抖音签名脚本
│   └── zhihu.js               # 知乎脚本
├── cmd_arg/                   # 命令行参数解析
│   └── arg.py                 # Typer CLI 参数定义
└── test/ & tests/             # 测试目录
```

---

## 3. 核心模块职责

### 3.1 入口与生命周期 (`main.py`)

| 组件 | 职责 |
|------|------|
| `CrawlerFactory` | 工厂模式，根据 `PLATFORM` 配置创建对应平台的 Crawler 实例 |
| `main()` | 异步主函数：解析命令行 → 初始化DB → 创建爬虫 → 启动爬虫 → 刷写Excel → 生成词云 |
| `async_cleanup()` | 异步清理：关闭CDP浏览器/浏览器上下文、关闭数据库连接 |
| `run()` (from `tools.app_runner.py`) | 应用运行器：注册信号处理、优雅退出、超时清理 |

**CrawlerFactory 映射表**:

| 配置值 | 平台 | 爬虫类 |
|--------|------|--------|
| `xhs` | 小红书 | `XiaoHongShuCrawler` |
| `dy` | 抖音 | `DouYinCrawler` |
| `ks` | 快手 | `KuaishouCrawler` |
| `bili` | B站 | `BilibiliCrawler` |
| `wb` | 微博 | `WeiboCrawler` |
| `tieba` | 百度贴吧 | `TieBaCrawler` |
| `zhihu` | 知乎 | `ZhihuCrawler` |

### 3.2 抽象基类 (`base/base_crawler.py`)

| 抽象类 | 核心方法 | 说明 |
|--------|----------|------|
| `AbstractCrawler` | `start()`, `search()`, `launch_browser()`, `launch_browser_with_cdp()` | 爬虫核心接口，所有平台爬虫必须实现 |
| `AbstractLogin` | `begin()`, `login_by_qrcode()`, `login_by_mobile()`, `login_by_cookies()` | 登录接口 |
| `AbstractStore` | `store_content()`, `store_comment()`, `store_creator()` | 数据存储接口 |
| `AbstractStoreImage` | `store_image()` | 图片存储接口 |
| `AbstractStoreVideo` | `store_video()` | 视频存储接口 |
| `AbstractApiClient` | `request()`, `update_cookies()` | API 客户端接口 |

### 3.3 平台爬虫层 (`media_platform/`)

每个平台目录结构一致，包含以下模块：

| 模块 | 职责 |
|------|------|
| `core.py` | 爬虫核心逻辑：启动浏览器、登录、搜索/详情/创作者三种模式的主流程 |
| `client.py` | API 客户端：封装平台 HTTP 接口调用，处理签名、请求重试 |
| `login.py` | 登录逻辑：二维码/手机号/Cookie 三种登录方式 |
| `field.py` | 枚举定义：搜索排序类型、内容类型等 |
| `help.py` | 辅助函数：URL 解析、数据提取 |
| `exception.py` | 自定义异常：`DataFetchError`, `LoginError` 等 |

**各平台爬虫核心流程**（以 `XiaoHongShuCrawler.search()` 为例）：

```
1. 初始化代理池（如启用）
2. 启动浏览器（CDP模式 或 标准Playwright模式）
3. 创建 API 客户端（从浏览器上下文提取Cookie）
4. 检测登录状态 → 未登录则执行登录流程
5. 遍历关键词列表：
   a. 智能增量检查（查询数据库已有数量）
   b. 分页搜索 → 获取搜索结果
   c. 过滤已存在帖子 → 并发获取帖子详情
   d. 存储帖子数据 → 下载媒体（如启用）
   e. 批量获取评论（如启用）
   f. 智能睡眠间隔
   g. 失败率/连续失败检查 → 超阈值则终止
6. 生成测试报告（如启用测试模式）
```

### 3.4 数据存储层 (`store/`)

采用**工厂模式**，根据 `config.SAVE_DATA_OPTION` 动态选择存储实现：

| 存储类型 | 实现类 | 说明 |
|----------|--------|------|
| `csv` | `XhsCsvStoreImplement` | CSV 文件存储 |
| `json` | `XhsJsonStoreImplement` | JSON 文件存储 |
| `jsonl` | `XhsJsonlStoreImplement` | JSONL 文件存储 |
| `db` | `XhsDbStoreImplement` | MySQL 数据库（SQLAlchemy ORM） |
| `sqlite` | `XhsSqliteStoreImplement` | SQLite 数据库（继承 DbStore） |
| `postgres` | `XhsDbStoreImplement` | PostgreSQL 数据库 |
| `mongodb` | `XhsMongoStoreImplement` | MongoDB 数据库 |
| `excel` | `XhsExcelStoreImplement` | Excel 文件存储 |

**存储流程**（以小红书为例）：

```
core.py 调用 xhs_store.update_xhs_note(note_item)
  → 数据标准化（提取用户信息、互动数据、图片列表等）
  → XhsStoreFactory.create_store() 根据配置创建存储实例
  → store.store_content(local_db_item) 执行实际存储
```

**数据库存储的 Upsert 逻辑**：
- 先查询记录是否存在（`content_is_exist`）
- 存在则更新（`update_content`）：更新互动数据（点赞、收藏、评论、分享数）
- 不存在则新增（`add_content`）：插入完整记录

### 3.5 数据库层 (`database/`)

| 组件 | 职责 |
|------|------|
| `db.py` | 数据库初始化入口，调用 `create_tables()` |
| `db_session.py` | SQLAlchemy 异步引擎和会话管理，支持 SQLite/MySQL/PostgreSQL |
| `models.py` | ORM 模型定义，所有平台的表结构 |
| `mongodb_store_base.py` | MongoDB 存储基类，提供 `save_or_update` 通用方法 |

**数据库会话管理**：
- `get_async_engine(db_type)` — 根据数据库类型创建/缓存异步引擎
- `create_tables(db_type)` — 自动建库建表
- `get_session()` — 异步上下文管理器，自动提交/回滚

**ORM 模型一览**：

| 平台 | 内容表 | 评论表 | 创作者表 | 其他表 |
|------|--------|--------|----------|--------|
| 小红书 | `xhs_note` | `xhs_note_comment` | `xhs_creator` | - |
| 抖音 | `douyin_aweme` | `douyin_aweme_comment` | `dy_creator` | - |
| 快手 | `kuaishou_video` | `kuaishou_video_comment` | - | - |
| B站 | `bilibili_video` | `bilibili_video_comment` | `bilibili_up_info` | `bilibili_contact_info`, `bilibili_up_dynamic` |
| 微博 | `weibo_note` | `weibo_note_comment` | `weibo_creator` | - |
| 贴吧 | `tieba_note` | `tieba_comment` | `tieba_creator` | - |
| 知乎 | `zhihu_content` | `zhihu_comment` | `zhihu_creator` | - |

### 3.6 缓存层 (`cache/`)

| 组件 | 职责 |
|------|------|
| `AbstractCache` | 缓存抽象基类，定义 `get()`, `set()`, `keys()` 接口 |
| `CacheFactory` | 缓存工厂，支持 `memory`（本地内存）和 `redis` 两种类型 |
| `ExpiringLocalCache` | 基于内存的过期缓存实现 |
| `RedisCache` | 基于 Redis 的缓存实现 |

### 3.7 代理层 (`proxy/`)

| 组件 | 职责 |
|------|------|
| `ProxyProvider` (base_proxy.py) | 代理提供者抽象基类 |
| `ProxyIpPool` | IP 代理池：加载代理、验证代理、自动刷新过期代理 |
| `IpInfoModel` (types.py) | 代理 IP 数据模型，包含过期时间检查 |
| `kuaidl_proxy.py` | 快代理实现 |
| `wandou_http_proxy.py` | 蜿豆HTTP实现 |
| `jishu_http_proxy.py` | 极速HTTP实现 |

**代理池工作流程**：
1. `create_ip_pool()` 创建代理池并加载代理列表
2. `get_proxy()` 随机提取一个代理并验证可用性
3. `get_or_refresh_proxy()` 每次请求前检查代理是否过期，过期则自动刷新

### 3.8 浏览器管理 (`tools/cdp_browser.py`, `tools/browser_launcher.py`)

| 组件 | 职责 |
|------|------|
| `CDPBrowserManager` | CDP 模式浏览器管理器：启动、连接、清理 |
| `BrowserLauncher` | 浏览器启动器：检测浏览器路径、启动进程、等待就绪 |

**两种浏览器模式**：

| 模式 | 配置 | 特点 |
|------|------|------|
| 标准模式 | `ENABLE_CDP_MODE=False` | Playwright 直接启动 Chromium，注入 stealth.min.js |
| CDP 模式 | `ENABLE_CDP_MODE=True` | 通过 CDP 协议连接用户本地 Chrome/Edge，反检测效果更好 |

CDP 模式支持两种连接方式：
- **启动新浏览器** (`CDP_CONNECT_EXISTING=False`)：自动检测并启动用户的 Chrome/Edge
- **连接已有浏览器** (`CDP_CONNECT_EXISTING=True`)：连接已开启远程调试的浏览器，反检测效果最佳

### 3.9 WebUI API 层 (`api/`)

| 组件 | 职责 |
|------|------|
| `api/main.py` | FastAPI 应用，注册路由、CORS、静态文件服务 |
| `routers/crawler.py` | 爬虫控制 API：启动/停止爬虫 |
| `routers/data.py` | 数据查询 API：查询爬取结果 |
| `routers/websocket.py` | WebSocket 实时日志推送 |
| `services/crawler_manager.py` | 爬虫进程管理器：以子进程方式启动 `main.py`，管理生命周期 |
| `schemas/crawler.py` | 请求/响应 Pydantic 模型 |

### 3.10 命令行参数 (`cmd_arg/arg.py`)

基于 Typer 的命令行参数解析，支持以下参数：

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--platform` | 平台选择 (xhs/dy/ks/bili/wb/tieba/zhihu) | `xhs` |
| `--lt` | 登录方式 (qrcode/phone/cookie) | `qrcode` |
| `--type` | 爬取类型 (search/detail/creator) | `search` |
| `--start` | 起始页码 | `1` |
| `--keywords` | 搜索关键词（逗号分隔） | 配置文件值 |
| `--max_count` | 最大爬取数量 | `100` |
| `--get_comment` | 是否爬取一级评论 | `false` |
| `--get_sub_comment` | 是否爬取二级评论 | `false` |
| `--headless` | 是否无头模式 | `false` |
| `--save_data_option` | 存储方式 | `sqlite` |
| `--init_db` | 初始化数据库表结构 | - |
| `--cookies` | Cookie 值 | - |
| `--specified_id` | 详情模式帖子ID列表 | - |
| `--creator_id` | 创作者模式ID列表 | - |
| `--test_mode` | 测试模式（不入库，生成HTML报告） | `false` |
| `--enable_ip_proxy` | 是否启用IP代理 | `false` |

---

## 4. 关键类与函数说明

### 4.1 核心爬虫类

#### `XiaoHongShuCrawler` (media_platform/xhs/core.py)

| 方法 | 说明 |
|------|------|
| `start()` | 主入口：初始化代理→启动浏览器→创建客户端→登录→分发爬取模式 |
| `search()` | 关键词搜索模式：分页搜索→获取详情→存储→获取评论 |
| `get_specified_notes()` | 详情模式：获取指定帖子详情和评论 |
| `get_creators_and_notes()` | 创作者模式：获取创作者信息和其帖子 |
| `_should_skip_keyword()` | 智能增量检查：查询数据库已有数量，计算还需爬取数量 |
| `_filter_search_items()` | 过滤搜索结果：排除非笔记项和已存在笔记 |
| `_store_note_detail()` | 存储单条笔记详情，支持搜索列表降级数据 |
| `get_note_detail_async_task()` | 异步获取笔记详情，支持 API→HTML 降级 |
| `batch_get_note_comments()` | 批量获取笔记评论 |
| `create_xhs_client()` | 创建小红书 API 客户端 |

#### `DouYinCrawler` (media_platform/douyin/core.py)

| 方法 | 说明 |
|------|------|
| `search()` | 关键词搜索：与XHS类似，但使用抖音搜索API |
| `get_specified_awemes()` | 详情模式：支持短链接解析 |
| `get_creators_and_videos()` | 创作者模式：获取创作者视频列表 |
| `get_aweme_media()` | 媒体下载：自动判断短视频/图文帖子类型 |

#### `BilibiliCrawler` (media_platform/bilibili/core.py)

| 方法 | 说明 |
|------|------|
| `search()` | 搜索分发：支持普通搜索和时间范围搜索 |
| `search_by_keywords()` | 普通关键词搜索 |
| `search_by_keywords_in_time_range()` | 时间范围搜索：按日期分片搜索 |
| `get_pubtime_datetime()` | 日期转时间戳工具 |
| `get_all_creator_details()` | 创作者详情：获取粉丝、关注、动态列表 |
| `get_bilibili_video()` | 视频下载 |

### 4.2 存储工厂

#### `XhsStoreFactory` (store/xhs/__init__.py)

```python
class XhsStoreFactory:
    STORES = {
        "csv": XhsCsvStoreImplement,
        "db": XhsDbStoreImplement,
        "postgres": XhsDbStoreImplement,
        "json": XhsJsonStoreImplement,
        "jsonl": XhsJsonlStoreImplement,
        "sqlite": XhsSqliteStoreImplement,
        "mongodb": XhsMongoStoreImplement,
        "excel": XhsExcelStoreImplement,
    }
```

每个平台的 `__init__.py` 都定义了类似的 StoreFactory 和数据标准化函数（如 `update_xhs_note`, `batch_update_xhs_note_comments`, `save_creator`）。

### 4.3 关键工具函数

#### `crawler_util.py`

| 函数 | 说明 |
|------|------|
| `check_and_adjust_crawler_count(store, keyword, max_count)` | 智能增量：查询数据库已有数量，返回还需爬取的数量和已有ID集合 |
| `is_db_storage(db_type)` | 判断存储类型是否为数据库（db/sqlite/postgres） |
| `smart_sleep()` | 智能睡眠：根据配置决定随机/固定间隔 |
| `find_login_qrcode(page, selector)` | 从页面提取登录二维码 |
| `show_qrcode(qr_code)` | 解析并显示base64二维码 |
| `convert_browser_context_cookies(browser_context, urls)` | 从浏览器上下文提取Cookie |
| `format_proxy_info(ip_proxy_info)` | 格式化代理信息为 Playwright/httpx 格式 |
| `generate_html_report(items, platform, keyword, output_path)` | 生成测试模式HTML报告 |

#### `app_runner.py`

| 函数 | 说明 |
|------|------|
| `run(app_main, app_cleanup, ...)` | 应用运行器：注册信号处理、优雅退出、超时清理 |

### 4.4 全局上下文变量 (`var.py`)

| 变量 | 类型 | 说明 |
|------|------|------|
| `request_keyword_var` | `ContextVar[str]` | 当前请求关键词 |
| `crawler_type_var` | `ContextVar[str]` | 当前爬取类型 (search/detail/creator) |
| `comment_tasks_var` | `ContextVar[List[Task]]` | 评论爬取任务列表 |
| `db_conn_pool_var` | `ContextVar[aiomysql.Pool]` | MySQL 连接池 |
| `source_keyword_var` | `ContextVar[str]` | 来源关键词（用于标记数据来源） |

---

## 5. 依赖关系

### 5.1 核心依赖

| 依赖 | 版本 | 用途 |
|------|------|------|
| `playwright` | 1.45.0 | 浏览器自动化 |
| `httpx` | 0.28.1 | 异步 HTTP 客户端 |
| `sqlalchemy` | >=2.0.43 | ORM 框架 |
| `aiosqlite` | >=0.21.0 | SQLite 异步驱动 |
| `aiomysql` | 0.2.0 | MySQL 异步驱动 |
| `asyncmy` | >=0.2.10 | MySQL 异步驱动（SQLAlchemy用） |
| `asyncpg` | >=0.31.0 | PostgreSQL 异步驱动 |
| `motor` | >=3.3.0 | MongoDB 异步驱动 |
| `redis` | ~=4.6.0 | Redis 客户端 |
| `fastapi` | 0.110.2 | WebUI API 框架 |
| `uvicorn` | 0.29.0 | ASGI 服务器 |
| `pydantic` | 2.5.2 | 数据模型验证 |
| `tenacity` | 8.2.2 | 重试机制 |
| `typer` | >=0.12.3 | CLI 框架 |
| `pandas` | 2.2.3 | 数据处理（B站时间范围搜索） |
| `jieba` | 0.42.1 | 中文分词（词云） |
| `wordcloud` | 1.9.3 | 词云生成 |
| `matplotlib` | 3.9.0 | 图表绘制 |
| `aiofiles` | ~=23.2.1 | 异步文件操作 |
| `openpyxl` | >=3.1.2 | Excel 文件操作 |
| `parsel` | 1.9.1 | HTML/XPath 解析 |
| `pyexecjs` | 1.5.1 | JavaScript 执行（签名计算） |
| `cryptography` | >=45.0.7 | 加密库 |
| `websockets` | >=15.0.1 | WebSocket 通信 |
| `python-dotenv` | 1.0.1 | 环境变量加载 |

### 5.2 模块间依赖关系

```
main.py → config/, cmd_arg/, media_platform/*, database/, tools/, var
media_platform/*/core.py → base/, config/, store/*, proxy/, tools/, var, model/
store/* → base/, database/, tools/, var, config/
database/ → config/, models
api/ → config/, cmd_arg/, store/*
cache/ → config/ (redis配置)
proxy/ → config/, tools/
tools/ → config/ (部分)
```

---

## 6. 配置体系

### 6.1 配置加载链

```
config/base_config.py          # 基础配置（所有平台通用）
  ├── config/db_config.py      # 数据库连接配置
  ├── config/xhs_config.py     # 小红书专属配置（SORT_TYPE, NOTE_TYPE, CREATOR_ID_LIST等）
  ├── config/dy_config.py      # 抖音专属配置
  ├── config/ks_config.py      # 快手专属配置
  ├── config/bilibili_config.py # B站专属配置
  ├── config/weibo_config.py   # 微博专属配置
  ├── config/tieba_config.py   # 贴吧专属配置
  └── config/zhihu_config.py   # 知乎专属配置
```

配置通过 `from .xxx_config import *` 方式汇聚到 `config` 命名空间，命令行参数可覆盖配置文件值。

### 6.2 关键配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `PLATFORM` | `"xhs"` | 目标平台 |
| `KEYWORDS` | 逗号分隔 | 搜索关键词 |
| `LOGIN_TYPE` | `"qrcode"` | 登录方式 |
| `CRAWLER_TYPE` | `"search"` | 爬取类型 |
| `CRAWLER_MAX_NOTES_COUNT` | `100` | 最大爬取帖子数量 |
| `SAVE_DATA_OPTION` | `"sqlite"` | 数据存储方式 |
| `ENABLE_CDP_MODE` | `True` | 是否启用CDP模式 |
| `CDP_CONNECT_EXISTING` | `True` | 是否连接已有浏览器 |
| `HEADLESS` | `False` | 是否无头模式 |
| `ENABLE_SMART_CRAWLER` | `True` | 是否启用智能增量爬取 |
| `ENABLE_GET_COMMENTS` | `False` | 是否爬取评论 |
| `ENABLE_GET_SUB_COMMENTS` | `False` | 是否爬取子评论 |
| `ENABLE_GET_MEIDAS` | `False` | 是否下载媒体文件 |
| `ENABLE_RANDOM_SLEEP` | `True` | 是否启用随机睡眠 |
| `CRAWLER_MIN_SLEEP_SEC` | `3` | 最小睡眠秒数 |
| `CRAWLER_MAX_SLEEP_SEC` | `8` | 最大睡眠秒数 |
| `CRAWLER_MAX_FAILURE_RATE` | `0.3` | 最大失败比例 |
| `CRAWLER_MAX_CONSECUTIVE_FAILURES` | `5` | 最大连续失败数 |
| `CRAWLER_MAX_EMPTY_PAGES` | `3` | 最大连续空页数 |
| `ENABLE_TEST_MODE` | `False` | 测试模式（不入库） |
| `ENABLE_IP_PROXY` | `False` | 是否启用IP代理 |
| `MAX_CONCURRENCY_NUM` | `1` | 最大并发数 |

### 6.3 环境变量

通过 `.env` 文件或环境变量配置数据库连接和代理密钥，参见 `.env.example`：

- MySQL: `MYSQL_DB_PWD`, `MYSQL_DB_USER`, `MYSQL_DB_HOST`, `MYSQL_DB_PORT`, `MYSQL_DB_NAME`
- Redis: `REDIS_DB_HOST`, `REDIS_DB_PWD`, `REDIS_DB_PORT`, `REDIS_DB_NUM`
- MongoDB: `MONGODB_HOST`, `MONGODB_PORT`, `MONGODB_USER`, `MONGODB_PWD`, `MONGODB_DB_NAME`
- PostgreSQL: `POSTGRES_DB_PWD`, `POSTGRES_DB_USER`, `POSTGRES_DB_HOST`, `POSTGRES_DB_PORT`, `POSTGRES_DB_NAME`
- 代理: `WANDOU_APP_KEY`, `KDL_SECERT_ID`, `KDL_SIGNATURE`, `KDL_USER_NAME`, `KDL_USER_PWD`, `jisu_key`, `jisu_crypto`

---

## 7. 项目运行方式

### 7.1 环境准备

```bash
# 1. 安装 uv (Python 包管理器)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 安装依赖
uv sync

# 3. 安装 Playwright 浏览器
uv run playwright install chromium

# 4. 配置环境变量
cp .env.example .env
# 编辑 .env 填入数据库连接信息和代理密钥

# 5. 初始化数据库（首次运行）
uv run python main.py --init_db sqlite
```

### 7.2 CLI 方式运行

```bash
# 基本运行（使用配置文件默认值）
uv run python main.py

# 指定平台和关键词
uv run python main.py --platform xhs --keywords "关键词1,关键词2" --type search

# 指定存储方式和爬取数量
uv run python main.py --platform dy --save_data_option sqlite --max_count 50

# 详情模式
uv run python main.py --platform xhs --type detail --specified_id "笔记URL1,笔记URL2"

# 创作者模式
uv run python main.py --platform bili --type creator --creator_id "创作者URL1,创作者URL2"

# 启用评论爬取
uv run python main.py --platform xhs --get_comment true --get_sub_comment true

# 测试模式（不入库，生成HTML报告验证）
uv run python main.py --platform xhs --test_mode

# Cookie 登录
uv run python main.py --platform xhs --lt cookie --cookies "your_cookie_string"
```

### 7.3 WebUI 方式运行

```bash
# 启动 API 服务
uvicorn api.main:app --port 8080 --reload

# 或直接运行
uv run python -m api.main
```

访问 `http://localhost:8080` 打开 WebUI 界面，可通过浏览器操作爬虫。

### 7.4 退出与清理

程序支持优雅退出：
- **Ctrl+C**: 第一次发送 SIGTERM，触发清理（关闭浏览器、刷写数据、关闭DB连接），最多等待 15 秒
- **第二次 Ctrl+C**: 强制退出
- 清理顺序：CDP浏览器进程 → 浏览器上下文 → 数据库连接

---

## 8. 数据流转

```
用户输入 (CLI/WebUI)
    │
    ▼
CrawlerFactory.create_crawler(platform)
    │
    ▼
Crawler.start()
    ├── 启动浏览器 (Playwright / CDP)
    ├── 登录 (二维码/手机/Cookie)
    ├── 创建 API Client (httpx + 浏览器Cookie)
    │
    ▼
Crawler.search() / get_specified_notes() / get_creators_and_notes()
    │
    ├── Client.search(keyword) → 平台API响应
    ├── 过滤已存在数据 (smart crawler)
    ├── 并发获取详情 (asyncio.gather + Semaphore)
    │
    ▼
Store 层数据标准化
    ├── 提取用户信息、互动数据、标签等
    ├── 生成 local_db_item 字典
    │
    ▼
StoreFactory.create_store() → 具体存储实现
    ├── CSV/JSON/JSONL → AsyncFileWriter 写入文件
    ├── Excel → ExcelStoreBase 写入 Excel
    ├── SQLite/MySQL/PostgreSQL → SQLAlchemy ORM (Upsert)
    └── MongoDB → MongoDBStoreBase (save_or_update)
```

---

## 9. 智能增量爬取机制

当 `ENABLE_SMART_CRAWLER=True` 且存储方式为数据库（db/sqlite/postgres）时，启用智能增量爬取：

1. 搜索前查询数据库中该关键词已有多少条记录
2. 计算还需爬取的数量：`adjusted_count = CRAWLER_MAX_NOTES_COUNT - existing_count`
3. 如果已满足数量，跳过该关键词
4. 搜索过程中过滤已存在的帖子ID，避免重复入库
5. 数据库存储采用 Upsert 策略：已存在则更新互动数据，不存在则新增

---

## 10. 容错与保护机制

| 机制 | 配置项 | 说明 |
|------|--------|------|
| 失败率保护 | `CRAWLER_MAX_FAILURE_RATE=0.3` | 失败比例超过30%时终止爬取 |
| 连续失败保护 | `CRAWLER_MAX_CONSECUTIVE_FAILURES=5` | 连续失败5次时终止爬取 |
| 空页保护 | `CRAWLER_MAX_EMPTY_PAGES=3` | 连续3页无新内容时考虑停止 |
| 请求间隔 | `CRAWLER_MIN/MAX_SLEEP_SEC` | 随机/固定睡眠，避免被封 |
| API降级 | XHS: API→HTML | API获取失败时降级为HTML解析 |
| 搜索列表降级 | XHS: `_store_note_detail` | 详情获取失败时使用搜索列表数据 |
| CDP降级 | CDP→标准模式 | CDP启动失败时自动回退到标准Playwright模式 |
| 重试机制 | `tenacity` | API请求自动重试 |
| 优雅退出 | `app_runner.py` | 信号处理+超时清理 |

---

## 11. 关于超出 CRAWLER_MAX_NOTES_COUNT 限制的帖子入库问题

**结论：是的，超出 `CRAWLER_MAX_NOTES_COUNT` 配置限制的帖子会顺利入库。`CRAWLER_MAX_NOTES_COUNT` 是一个"软限制"，实际入库数量可能超过设定值。**

### 原因分析

所有平台的搜索爬取都采用**分页获取**模式，计数检查发生在**整页处理完毕之后**，而非每条数据处理之前：

1. **小红书** (`xhs/core.py` search方法)：
   - API 每页返回最多 20 条数据
   - 当前页所有帖子通过 `asyncio.gather` 并发获取详情后，逐条存储
   - `adjusted_max_count -= 1` 在每条成功存储后执行
   - `if adjusted_max_count <= 0: break` 检查在**整页处理完毕后**才执行
   - 因此最后一页的 20 条数据会全部入库，即使已超出目标数量
   - 此外，XHS 强制最低爬取数量为 20：`if config.CRAWLER_MAX_NOTES_COUNT < xhs_limit_count: config.CRAWLER_MAX_NOTES_COUNT = xhs_limit_count`

2. **抖音** (`douyin/core.py` search方法)：
   - API 每页返回 10 条数据
   - 逐条存储，`adjusted_max_count -= 1` 在每条存储后执行
   - 但 `adjusted_max_count <= 0` 的检查在 for 循环**外部**（while 循环层级）
   - 因此当前页所有数据都会被处理和存储

3. **B站** (`bilibili/core.py` search_by_keywords方法)：
   - API 每页返回 20 条数据
   - 所有视频详情并发获取后逐条存储
   - `adjusted_max_count <= 0` 检查在 for 循环**外部**
   - 最后一页超出部分同样会入库

### 超出数量的估算

| 平台 | 每页数量 | 最大可能超出 |
|------|----------|-------------|
| 小红书 | 20 | 最多超出 ~19 条 |
| 抖音 | 10 | 最多超出 ~9 条 |
| B站 | 20 | 最多超出 ~19 条 |
| 快手 | 视API而定 | 视API而定 |
| 微博 | 视API而定 | 视API而定 |
| 贴吧 | 视API而定 | 视API而定 |
| 知乎 | 视API而定 | 视API而定 |

### 这是 Bug 还是 Feature？

这更接近于一个**设计取舍**而非 Bug。分页 API 无法精确控制返回数量，在"多存几条"和"少存几条但需要更复杂的逐条中断逻辑"之间，项目选择了前者——确保数据完整性优先于精确计数。如果需要精确控制入库数量，可以在存储层增加数量门控检查。
