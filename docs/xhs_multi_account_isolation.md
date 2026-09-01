# 小红书多账号隔离并行运行

更新日期：2026-08-30

Dashboard 现在把“小红书账号”作为一级运行单元。目标是让不同账号可以同时处理各自的搜索或评论队列，同时把登录态、内容写入、日志、风控和故障尽可能限制在单个账号内。这里的多账号能力用于管理用户已合法登录的账号，不会自动轮换账号来规避验证码或平台风控。

## 隔离边界

| 层面 | 隔离方式 | 强制约束 |
|---|---|---|
| 账号身份 | `~/Library/Application Support/MediaCrawler/database/task_manager.db` 中的 `xhs_accounts` 注册表 | 任务只保存 `account_id`；运行时的 Profile、浏览器和数据库路径由注册表覆盖，HTTP 请求不能临时改路径 |
| 登录态 | 每账号一个 `~/Library/Application Support/MediaCrawler/browser-data/...` Profile | Profile 模板必须位于应用状态目录的 `browser-data` 下；解析后的物理目录必须唯一 |
| 浏览器 | 明确指定隔离 Chromium | 评论任务关闭 CDP；显式 CDP 也只能使用隔离 Chromium，禁止系统 Chrome 和无路径回退 |
| 并发 | 同账号 search/comment 共用一个串行 slot，不同账号可并行 | 默认全局最多 2 个活跃账号，可用 `MEDIACRAWLER_MAX_PARALLEL_XHS_ACCOUNTS` 调整 |
| 物理运行锁 | Profile 对应一个 `flock` 文件锁 | 即使两个 Dashboard 实例竞争，同一物理 Profile 也只能被一个 worker 打开 |
| 内容数据 | 新账号使用 `database/accounts/<account_id>/sqlite_tables.db` | 每个 SQLite 独立 WAL 和 busy timeout；A 库被锁或损坏不会阻塞 B 库 |
| 风控 | 每个注册 Profile 一套持久状态机、启动预算和运行租约 | A 的 461/471 只冷却或熔断 A；worker 每 30 秒续租，旧任务完成不能释放新 owner |
| 日志 | `dashboard/logs/accounts/<account_id>/<task_id>*.log` | wrapper、crawler runtime 和其他账号不再竞争全局轮转文件 |

任务表仍是共享的调度控制面，但启用了 WAL、30 秒 busy timeout，并为 `(account_id, status)` 建立索引。内容数据面不共享，避免多进程在一个内容库内互相锁表。

## 注册账号

账号管理只提供本机 CLI，不开放无认证的 HTTP 写接口。Dashboard 默认只监听 `127.0.0.1`，页面只读取经过脱敏的账号列表。

```bash
# 查看账号，不输出绝对 Profile、数据库或浏览器路径
.venv/bin/python dashboard/account_registry.py list

# 注册一个全新隔离账号；浏览器必须是隔离 Chromium 可执行文件
.venv/bin/python dashboard/account_registry.py add research-b \
  --display-name "研究账号 B" \
  --browser-path "/absolute/path/to/isolated/Chromium"

# 登录完成后启用/暂停该账号
.venv/bin/python dashboard/account_registry.py set-enabled research-b true
.venv/bin/python dashboard/account_registry.py set-enabled research-b false
```

注册动作会分配独立 Profile 模板和独立内容库目录，但不会启动浏览器、抓取内容或复制 cookies。登录应由操作者在该账号自己的隔离 Profile 中完成。

## 调度语义

1. Scheduler 每轮扫描所有 `start_mode=auto` 队列。
2. 同一账号存在 `starting/running/stopping` 的 search 或 comment 时，该账号不会再启动第二项。
3. 一个账号被 cooldown、locked、启动间隔或预算拒绝，不会阻塞其他账号的队列。
4. 不同账号只有在 Profile 路径和内容库路径都不同的情况下才能并行。
5. 全局并行账号数达到上限后，其余任务保持 `pending`，下一轮再评估。
6. 搜索任务优先于同账号的评论任务，以满足恢复探针和评论前搜索会话要求。

账号刚注册时风控状态固定为 `canary`，必须完成两次干净搜索探针后才进入正常状态。人工登录窗口仍占用 Chromium 原生 Profile 锁；Dashboard 会在预留启动预算前拒绝任务，避免把一次必然失败的启动计入账号预算。

这仍然是“每个 Dashboard 任务一个 Playwright worker”。standard mode 无法安全附着到上一任务留下的 Playwright 浏览器，因此跨任务不能伪装成浏览器复用；减少启动次数依靠合并同类搜索、评论候选批处理和账号级间隔，而不是把孤儿浏览器留在后台。

## 历史数据迁移

- 首次升级时账号 `02` 先以 `legacy_shared` 读取历史 `~/data/datasets/mediacrawler/sqlite_tables.db`；确认无活跃任务后，运行 `.venv/bin/python dashboard/account_registry.py migrate-default`，通过 SQLite 在线备份生成 `~/data/datasets/mediacrawler/accounts/02/sqlite_tables.db`，完整性校验通过后再原子切换注册表。
- 迁移只复制并改写账号路由，不移动、不改写、不删除原历史汇总库；原库继续作为回滚和跨账号历史审计源，不能再作为新任务的并行写入目标。
- 能从历史任务 Profile 明确识别的账号会回填到任务的 `account_id`。
- 缺失或无法识别 Profile 的历史任务标记为 `legacy-default` 或 `legacy-<hash>`，不会猜成账号 `02`。
- 除默认兼容账号外，历史共享库账号默认禁用；必须建立 dedicated 账号路由后才可参与并行。
- 历史 `crawler_account` 只代表旧实现留下的首次写入信息，不能反推“哪些账号都看过该帖子”。本次迁移不伪造历史 provenance。

回滚时可停止 Dashboard，把分支切回基线提交 `47ab9b39d`。本次数据库变更均为新增表/列/索引；旧内容库未移动或删除，新账号的独立库保留在 `database/accounts/`，可单独归档。

## 验收与故障演练

发布前必须通过以下不联网测试：

- A 运行时 B 可启动；A 的第二个 search/comment 不能启动；全局并发上限有效。
- A cooldown 或 461/471 不改变 B 的状态、探针计数或启动预算。
- 父进程故意提供错误账号、Profile、数据库、浏览器和 CDP 环境时，worker 仍使用注册表路由。
- 同一 Profile 的字符串别名注册失败；同 Profile 的两个进程只有一个能拿到文件锁。
- worker 心跳延长 lease；旧任务的迟到完成不能释放新 owner；取消只释放被取消任务；wrapper 异常退出后会回收进程组和调度槽。
- 锁住 A 的内容库后，B 的内容库仍可写；A/B 的 runtime 日志路径不同。
- 旧任务回填 unknown 而不是默认猜号；账号 `02` 的 working copy 与原历史库都通过完整性校验。

测试命令：

```bash
.venv/bin/python -m pytest -q \
  tests/test_account_registry.py \
  tests/test_multi_account_isolation.py \
  tests/test_risk_policy.py \
  tests/test_risk_scheduler.py \
  tests/test_comment_task_lifecycle.py \
  tests/test_crawl_task_lifecycle.py
```

## 已知边界

- CAPTCHA 仍然不能无人值守解决。触发后账号安全暂停并通知，其他账号可继续；系统不会自动换号、换代理或绕过验证。
- 独立内容库优先保证故障隔离，Dashboard 当前按账号查看数据；跨账号的只读汇总/去重层不在 crawler 写路径中。
- 默认并发 2 是本地资源上限，不是平台安全承诺。每个账号仍执行自己的 60/90 分钟间隔、12 小时启动预算和 461/471 状态机。
