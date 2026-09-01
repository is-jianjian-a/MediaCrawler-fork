# XHS 多账号数据架构

## 目标

小红书账号身份、浏览器 Profile、内容数据库、任务运行和内容来源必须分别建模。
`crawler_account` 是旧数据的单值来源标签，不是账号所有权，也不能用于运行隔离。

## 控制库

控制元数据保存在 `~/Library/Application Support/MediaCrawler/database/task_manager.db`：

- `xhs_accounts`：兼容账号注册表。`record_kind=account` 才是可运行账号；
  `historical_placeholder` 仅供旧任务审计。
- `xhs_browser_profiles`：浏览器 Profile 的规范化物理路径和状态。
- `xhs_data_stores`：内容库目录。类型包括账号工作库、历史聚合库、历史副本和归档。
- `xhs_store_routes`：账号、Profile 与 Store 的显式执行路由。
- `xhs_runs`：每次任务启动或重试对应一个独立 Run，保存完整配置快照和终态。
- `tasks.current_run_id` / `crawl_tasks.current_run_id`：旧任务表到最新 Run 的兼容指针。

历史任务导入为 `run_origin=legacy_task_snapshot`。无法证明真实账号的旧任务使用
`account_id=NULL` 和 `attribution_status=legacy_unattributed`，不得猜成默认账号。

## 内容库

每个启用账号只写自己的工作库。新写入在同一个内容库事务内同时维护：

- `xhs_note` / `xhs_note_comment`：按平台 ID 去重的最新实体数据。
- `xhs_note_observation`：Run、账号和关键词对帖子的观测记录。
- `xhs_comment_observation`：Run、账号对一级或子评论的观测记录。
- `xhs_note_keyword_hit`：旧搜索命中投影，继续兼容现有审计和 Dashboard 查询。

Observation 与实体同库是为了保持逐篇、逐页增量提交的原子性。Observation 中保存
`run_id/profile_id/store_id/route_id`，控制库负责 Run 和路由定义。历史聚合库和归档库
保持只读，不创建 Observation 表。

## Catalog

`/api/xhs-data-catalog` 从控制库读取已注册 Store，并以只读方式附加内容库：

1. 账号工作库优先；
2. 历史聚合库次之；
3. 原始归档最后；
4. 帖子、评论和关键词命中按业务唯一键去重。

账号 02 当前 Store 是历史聚合库的完整副本，因此标记为
`legacy_seeded_working`，并记录 `seed_store_id` 和 `seed_cutoff`。其中早于 cutoff 的
行不能解释为账号 02 独占数据。

## 安全约束

- 只有 `record_kind=account` 的记录进入账号选择器和并行风控预算。
- 一个启用路由必须独占 Profile 和可写 Store。
- 自动化只使用显式隔离 Chromium；禁止回退系统 Chrome。
- 新任务必须携带 `RUN_ID/PROFILE_ID/STORE_ID/ROUTE_ID`。
- 不根据 Profile 文件夹名称、Cookie 数量或 `crawler_account` 猜测真实账号。
- 历史主库、归档库和旧任务配置不做原地重写。

## 回滚

迁移前应使用 SQLite backup API 把控制库备份到
`~/Library/Application Support/MediaCrawler/database/archive/`，并记录源库、目标库、
时间和 `integrity_check` 结果。回滚代码时必须同步恢复匹配版本的控制库；历史内容库
不做原地改写，新 Observation 表只在账号工作库初始化 schema 时创建。
