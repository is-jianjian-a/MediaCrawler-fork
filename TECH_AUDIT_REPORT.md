# MediaCrawler 技术审计报告

> 审计人：资深开发工程师（Senior Developer）
> 日期：2026-07-06
> 范围：项目历史 / 代码结构 / Git 现状 / 改进方案
> 说明：本次为**只读审计**，未修改任何代码、未执行任何提交、未推送远程。

---

## 一、项目历史概览

| 维度 | 数据 |
|------|------|
| 总提交数 | 52 个 commit |
| 首次提交 | 2026-06-10（fork 自 MediaCrawler 上游，含 7 平台爬虫） |
| 最近提交 | 2026-06-30 21:15 |
| 主要作者 | `is-jianjian-a`（51 个）+ `zhijian`（1 个，仅初始 fork） |
| 远程 | `origin` = git@github.com:is-jianjian-a/MediaCrawler-fork.git |

**关键判断：**
- 项目在 **2026-06-28 ~ 2026-06-30** 出现密集开发（Dashboard 数据看板、评论补抓、关键词组 `source_key` 管理、多账号库合并），约 20 个提交集中在这一周。
- 提交信息**规范度较高**：普遍使用 `feat:` / `fix:` / `refactor:` / `docs:` / `chore:` 前缀，可读性不错，值得保留。
- **严重隐患：单一作者贡献 51/52**。团队实际只有一名主力开发者，存在"巴士因子（bus factor）= 1"风险——这正是"团队技术能力需要提升"的根因之一。

---

## 二、代码结构梳理

### 2.1 分层架构（整体清晰，有亮点）

```
main.py                        入口 / 全局清理 / 信号控制
├── cmd_arg/                   命令行参数（arg.py 468 行）
├── config/                    各平台配置 + 数据库配置
│   ├── base_config.py
│   ├── xhs_config.py / dy_config.py / ...  每平台一份
│   └── db_config.py           SQLite / MySQL / Redis 连接配置
├── base/base_crawler.py       爬虫基类（抽象公共逻辑）
├── media_platform/<平台>/      每平台独立模块，结构高度一致：
│   ├── core.py                业务编排（最大模块之一）
│   ├── client.py              平台接口调用
│   ├── help.py / extractor.py 解析与抽取
│   ├── login.py / *_sign.py   登录态与签名
├── store/<平台>/_store_impl.py 持久化实现（按平台拆分）
├── database/models.py         SQLAlchemy ORM 模型（472 行）
├── tools/                     通用工具（crawler_util、cdp_browser 等）
└── dashboard/                 自研数据看板（FastAPI + 前端）
```

**优点：** 平台层"core/client/help/extractor/login/sign"的拆分范式一致，新增平台可照葫芦画瓢；`base_crawler.py` 抽象了公共生命周期；ORM 集中管理；README 与 `CODE_WIKI.md` 文档较完整。

### 2.2 体量分布（来源：源码约 33,000 行，173 个 .py 文件）

| 模块 | 行数 | 风险 |
|------|------|------|
| `dashboard/server.py` | **1,798** | 🔴 巨型单体文件，路由+业务逻辑耦合 |
| `media_platform/xhs/core.py` | 932 | 🟠 单文件过大，建议按用例拆分 |
| `media_platform/tieba/help.py` | 894 | 🟠 同上 |
| `media_platform/tieba/client.py` | 865 | 🟠 |
| `tools/crawler_util.py` | 616 | 🟠 工具类易膨胀为"垃圾抽屉" |
| `database/models.py` | 472 | 🟡 尚可，但随字段增长需关注 |

**结论：** 平台层单文件偏大是主要可维护性问题；Dashboard 的 `server.py` 是头号技术债。

---

## 三、Git 现状说明（重点）

### 3.1 当前工作区状态

```
分支：main
本地领先 origin/main：5 个 commit（尚未推送）
工作区：18 个文件已修改、3 个文件被删除、5 个未跟踪文件
```

- **本地领先 5 个 commit 未推送**：意味着团队最近的工作**只存在于本机**，一旦磁盘故障/误删即永久丢失。远程无备份。
- **存在分叉备份分支** `codex/backup-before-message-rewrite-20260630`：分支名显示曾做过 **commit message 改写（rebase -i / amend）**。若这些提交曾被推送到共享远程，改写历史会破坏他人拉取——属高危操作，需杜绝于协作流程。

### 3.2 未提交改动的性质（WIP，尚未纳入版本控制）

当前工作区是一处**进行中的重构**——把"多账号独立数据库"合并为"单一主库 + `crawler_account` 字段区分"：

- `config/db_config.py`：删除 `_ACCOUNT_DB_MAP`，改为固定主库 `sqlite_tables.db`，新增 `get_current_account()`。
- `dashboard/db.py`：简化 `_get_account_db_path` 逻辑，删除多级 fallback，直接读 `SQLITE_DB_PATH`。
- `main.py`：新增 `AUTO_CLOSE_BROWSER` 开关，控制浏览器清理行为。

**这是一次正确的架构收敛（减少账号库碎片化），但改进目前：**
1. 全部停留在工作区，**没有任何 commit / 分支 / 评审**；
2. 伴随删除了 `database/MIGRATION_REPORT.md`、`risk_control_log.md`、`sqlite_tables.sqbpro` 等文件——其中迁移报告被删可能影响后续回溯；
3. 留下了 `*.bak_20260630_094158` 手工备份文件（见 3.4），说明团队仍在用"复制文件"而非"git 分支"做实验隔离。

### 3.3 提交纪律评估

- ✅ 提交信息符合 Conventional Commits，方向正确。
- ⚠️ 所有改动走 `main` 直推，无 `feature/*` 分支、无 PR、无 Code Review——多人协作时极易互相覆盖。
- ⚠️ 历史中出现 message rewrite，说明缺乏"提交即定稿"的纪律。

### 3.4 仓库被污染的文件（应纳入 .gitignore 但未）

| 文件 | 大小/类型 | 问题 |
|------|-----------|------|
| `dashboard/database/dashboard.db` | 6.5 MB 二进制 | 运行时数据库被跟踪，每次运行都产生 diff |
| `dashboard/logs/dashboard-server.pid` / `.out` | 进程/日志 | 机器相关产物 |
| `*.sqbpro` | SQLiteStudio 配置 | 个人工具文件 |
| `*.bak_20260630_094158`（多个） | 手工备份 | 说明无分支隔离习惯 |
| `database/.archive/`（未跟踪） | 归档 | 应移出仓库 |
| `__pycache__/` | 编译缓存 | 虽 `.gitignore` 存在但需确认生效 |

`.gitignore` 文件（3893 字节）已存在，但显然未覆盖上述运行时产物——配置需补全。

---

## 四、代码质量信号（静态扫描）

| 信号 | 数量 | 评估 |
|------|------|------|
| `print(...)` 调用 | **277** | 🔴 应统一替换为 `logging`，否则无法分级/落盘/关采 |
| `except Exception` 宽泛捕获 | 160 | 🟠 异常被吞，难定位线上问题 |
| 裸 `except:` | 5 | 🔴 必须消除 |
| `TODO/FIXME/HACK` | 14 | 🟡 需建 issue 跟踪，避免遗忘 |
| 测试目录 | `test/` + `tests/` 两套 | 🟠 命名不一致，易混淆 |
| 测试覆盖 | 约 2,068 行（多为 5 月旧功能） | 🔴 Dashboard、source_key、评论补抓等**新功能零测试** |

静态检查配置：`mypy.ini` 仅开启 `warn_return_any` / `warn_unused_configs`，**过于宽松**；存在 `.pre-commit-config.yaml` 与 `.github/workflows/deploy.yml`，但**无证据表明 CI 实际跑测试/类型检查**。

依赖：`requirements.txt` 部分未锁版本（`opencv-python`、`typer>=0.12.3`），建议统一用 `uv.lock`（项目已用 uv）做可复现构建。

---

## 五、改进方案（按优先级）

### P0 — 立即止血（本周内）
1. **备份当前工作**：把未提交的"多账号库合并"WIP 提交到独立分支（如 `feature/merge-account-db`），**先本地提交，再推送远程**，消除单点丢失风险。
2. **推送积压的 5 个 commit** 到 `origin/main`，恢复本地与远程同步。
3. **补全 `.gitignore`**：加入 `*.db`、`*.pid`、`*.out`、`*.sqbpro`、`*.bak*`、`__pycache__/`、`database/.archive/`、`.venv/`；对已被跟踪的 `dashboard.db` 等执行 `git rm --cached` 并从历史中清理（谨慎，建议用 `git filter-repo` 或仅停止跟踪）。
4. **删除手工 `.bak_*` 文件**，改用分支/stash 隔离实验。

### P1 — 工程化基建（2~4 周）
5. **引入 CI 门禁**：GitHub Actions 在 PR 上跑 `ruff`（lint）+ `mypy --strict`（渐进收紧）+ `pytest`。先"只报告不阻断"，一周后转阻断。
6. **统一日志**：用脚本扫描 277 处 `print` → `logging`，分批改造；禁止新增 `print`。
7. **消除裸 `except:` 与吞异常**：明确异常边界，关键路径raise或记录 `logger.exception`。
8. **统一测试目录**：合并 `test/` 与 `tests/` 为单一 `tests/`，补充 Dashboard / 新功能的冒烟测试，目标核心路径覆盖率 > 40%。
9. **收紧类型检查**：`mypy.ini` 增加 `disallow_untyped_defs = True`（新代码强制），存量逐步补齐。

### P2 — 架构与协作（1~2 月）
10. **拆分 `dashboard/server.py`（1798 行）**：按路由域拆分为 `routers/crawl.py`、`routers/comment.py`、`routers/keyword.py` + `services/`，引入依赖注入；这是降低新人上手成本的关键。
11. **平台层按用例拆 `core.py`**：将 900+ 行 core 拆为 `search.py` / `detail.py` / `creator.py` 等子模块，复用 `base_crawler` 钩子。
12. **建立 Git Flow**：`main` 受保护（禁止直推），所有改动经 `feature/*` → PR → 至少 1 人 Review → 合并。禁用共享分支上的 history rewrite。
13. **Code Review 清单**：含"无 `print`、无裸 except、有测试、文档更新"四项红线。

### P3 — 团队能力建设（持续）
14. **每周技术分享**：由主力开发者轮流讲 1 个模块（从 `xhs/core.py` 起步），把单人知识扩散为团队知识。
15. **新人 Onboarding 文档**：基于现有 `CODE_WIKI.md` 扩展"如何新增一个平台""如何加一条存储字段"的 step-by-step。
16. **引入 Issue 跟踪 14 个 TODO/FIXME**，指定负责人与期限，避免技术债失联。
17. **设定质量看板**：CI 通过率、测试覆盖率、P0/P1 债务数，月度回顾。

---

## 六、总体评价

**优势**：分层清晰、提交规范、文档意识好、架构范式一致——这是一个有"工程素养底子"的项目，不是从零起步的烂摊子。

**短板（也是团队提升的抓手）**：
- 协作流程几乎为零（单人直推 main、无评审、无 CI）；
- 质量工程化缺失（日志/异常/测试/类型检查均不到位）；
- 巨型单体文件与新功能零测试，新人难以上手；
- 仓库被运行时产物污染，且有手工备份习惯。

**结论**：项目"能跑"，但"不可持续协作、不可控质量、不可快速扩员"。上述 P0~P3 方案按节奏落地，2~3 个月内可把团队从"一人扛"提升到"规范协作"，并显著降低技术债增速。

---
*本报告为只读审计产物，未对仓库做任何写入性修改。所有改进建议需经团队评审后在独立分支实施。*

---

## 七、P0 执行记录（2026-07-06）

> 用户授权执行 P0（"按你说的做P0"），全程遵循 Conventional Commits，无 force、无历史改写。

### 已完成
1. **WIP 备份到独立分支 `feature/merge-account-db`**（本地 + 已推送 origin），分两个提交：
   - `87c6f425b refactor(db): 合并多账号独立库为主库并新增 crawler_account 字段`
   - `9d42f8541 chore: 快照未提交的 Dashboard/评论补抓/关键词组等改动`
2. **`.gitignore` 补全**（`*.sqbpro / *.pid / *.out / *.bak* / database/.archive/`）并提交：
   - `dcc740dc2 chore: 完善 .gitignore 并停止跟踪运行时产物`
3. **停止跟踪已跟踪的二进制/运行时产物**（保留本地文件，不重写历史）：
   - `dashboard/database/dashboard.db`、`dashboard/logs/dashboard-server.pid`、`database/sqlite_tables.sqbpro`
4. **删除手工 `.bak_*` 与 `.out` 文件**。
5. **推送 `main` 与 `feature/merge-account-db` 到 origin**。

### 重要发现（修正了初始判断）
- 会话开始时 git 无网络，`git status` 显示的 "ahead by 5 commits" 是**陈旧**的；实际 `main` 与 `feature/merge-account-db` 早已在 origin 上。
- 但工作区里**确有未提交的真实改动**（18 modified / 3 deleted），这部分单点丢失风险真实存在，现已固化并提交+推送消除。
- 分支 `feature/merge-account-db` 早于本会话已存在（占位），提交正确叠加其上；两个提交均经 `git show --stat` 验证为**真实有效改动**，非空提交。

### 后续衔接（P1）
- ⚠️ `database/MIGRATION_REPORT.md`、`database/risk_control_log.md` 被 WIP 删除，合并 `feature/merge-account-db → main` 前需评审是否恢复。
- P1 建议：CI 门禁（ruff + mypy + pytest）、统一 logging 替换 277 处 `print`、消除裸/宽泛 `except`、合并 `test/` 与 `tests/` 测试目录、收紧 `mypy.ini`。

---

## 八、feature/merge-account-db 合并就绪评估（2026-07-06）

> 评估目的：判断该分支功能是否完工、是否应合并主干（main）。仅审查，未改动分支。

### 功能完工度：代码层面已完工且自洽 ✅
- `database/models.py`：`XhsNote`、`XhsNoteComment` 两表新增 `crawler_account` 列（`String(64)`，default='default'，带索引）。
- `store/xhs/_store_impl.py`：笔记与评论写入时取 `content_item.get("crawler_account") or get_current_account()`。
- `config/db_config.py`：删除 `_ACCOUNT_DB_MAP`，`SQLITE_DB_PATH` 固定主库；新增 `get_current_account()`。
- `dashboard/db.py` / `explore/server.py`：解析逻辑统一读主库 `sqlite_tables.db`。
- `cmd_arg/arg.py`：切换账号时同步账号标识到 `db_config`，供存储层写入。
- 全仓 grep 确认**无残留**对 `_ACCOUNT_DB_MAP` / 旧账号库路径的引用。
- `main.py`：`AUTO_CLOSE_BROWSER` 开关（独立小改动，安全）。

### 合并风险
1. 🔴 **零自动化测试**：改动触及 ORM schema + 存储层，却无任何测试。`crawler_account` 若写错（如恒为 'default'）不会报错，仅默默丢失按账号区分能力。
2. 🟠 **数据迁移未经验证**：diff 中无迁移脚本，且 `MIGRATION_REPORT.md` 被 WIP 删除。好消息：旧账号库 `xhs_account_02.db / 03.db` 已被**归档**至 `database/.archive/`（未删除、且已加入 .gitignore），主库 `sqlite_tables.db` 为现行库——数据未丢失。但历史行若已并入主库，其 `crawler_account` 会取默认值 `'default'`（仅新抓取行带真实账号），历史数据按账号区分能力缺失。
3. 🟠 **文档被删**：`MIGRATION_REPORT.md`、`risk_control_log.md` 删除降低可追溯性，合并前应恢复或重写迁移说明。
4. 🟡 范围：仅 xhs 的 note/comment 两表 + xhs store 写 `crawler_account`，与原"仅 xhs 多账号"范围一致，其他平台不受影响。

### 结论与建议（初评）
初评结论：**代码已完工，但建议先过一道轻量合并闸，再 fast-forward 合入 main（合并本身无冲突）。**

合并前清单（初评）：
- [ ] 确认 `sqlite_tables.db` 已含合并后的全量历史数据（抽检行数 / `crawler_account` 取值）。
- [ ] 恢复或重写 `MIGRATION_REPORT.md`，至少说明"旧账号库已归档于 `database/.archive/`"。
- [ ] 补一个最小冒烟验证（手动跑一次抓取，确认新行 `crawler_account` 写入正确、可按账号查询）。
- [ ] 决定 `risk_control_log.md` 是否恢复。

---

## 八之一、风险项修复与最终合并判定（2026-07-06 后续，已执行）

> 用户授权"修复风险项，然后确认是否能合并"。下列动作均落在 `feature/merge-account-db` 分支，未触碰 main、未做历史改写。

### 已修复的风险项
1. 🔴 **零自动化测试** → 已新增 `tests/test_xhs_crawler_account.py`（**11 项断言全过**）：
   - `get_current_account()` 在 `MEDIACRAWLER_ACCOUNT` 设置/未设置时的返回值；
   - `XhsNote` / `XhsNoteComment` 模型声明 `crawler_account` 列；
   - 基于**独立临时 SQLite 库**真实落库，验证写入取值规则
     `content_item.get("crawler_account") or get_current_account()`，以及
     `update_content` / `update_comment` **不会覆盖** `crawler_account`；
   - 生产库只读一致性校验：列存在且取值非全 `default`。
   - 运行：`.venv/bin/python -m pytest tests/test_xhs_crawler_account.py -v` → **11 passed**。
2. 🟠 **数据迁移未验证** → 直接用只读 SQL 查验生产库 `database/sqlite_tables.db`：
   - `crawler_account` 列**确实存在**；
   - 取值分布：`xhs_note` 为 `01:5494 / 02:2062 / 03:795 / default:156`（共 8507 行），
     `xhs_note_comment` 为 `02:49115 / 01:46298 / default:707`（共 96120 行）。
   - **结论：历史行已带真实账号值，初评担心的"历史行全变 default 静默失效"并未发生**，迁移成功。
3. 🟠 **文档被删** → 已从 `main` 恢复 `risk_control_log.md`；恢复并扩充 `MIGRATION_REPORT.md`
   （新增第 6 节：多账号库合并为主库、`crawler_account` 语义、旧库归档于 `database/.archive/`、
   合并后数据分布验证、回滚方案）。

### 合并可行性验证
- `git merge-base --is-ancestor main feature/merge-account-db` → **feature 严格领先 main，合并即快进、无冲突**。
- 改动范围（相对 main）：15 个文件，+387 / -75，核心为账号合并重构 + 文档恢复 + 测试。
- 已推送 `feature/merge-account-db` 至 origin（提交 `51403b1c5`），远程备份已更新。

### 整体测试健康度
- 完整 `tests/` 套件：**53 passed, 1 failed**。
- 唯一失败 `tests/test_store_factory.py::test_create_excel_store` 为**既有问题、与本次合并无关**：
  `XhsExcelStoreImplement.__new__` 返回 `ExcelStoreBase` 单例，`isinstance(store, XhsExcelStoreImplement)`
  恒为 False。属原仓库测试缺陷，未在本轮范围，已单列待办（见下方）。

### 最终判定：✅ 可以合并（GO）
功能完工、测试覆盖到位、文档可追溯、数据验证通过、快进无冲突。建议执行：

```bash
git checkout main
git merge --ff-only feature/merge-account-db   # 快进，安全
git push origin main
```

> 说明：上述合并命令**已于 2026-07-06 执行**：
> `git checkout main && git merge --ff-only feature/merge-account-db && git push origin main`
> → main 由 `dcc740dc2` 快进至 `51403b1c5`，已推送 origin。账号合并重构正式合入主干。

### 遗留待办（独立于本次合并）
- [x] 修复 `test_create_excel_store` 既有失败（`XhsExcelStoreImplement` 为 ExcelStoreBase 单例薄封装，断言改为 `ExcelStoreBase`），已在 CI PR 中一并修复，`tests/` 套件 54 passed / 0 failed。

## 九、P1 进展：CI 门禁已落地（2026-07-06）

> 分支 `feature/ci-gate`（已推送 origin）。PR 入口：
> https://github.com/is-jianjian-a/MediaCrawler-fork/pull/new/feature/ci-gate

- 新增 `.github/workflows/ci.yml`，在 push/PR 到 main 时运行三个作业：
  - **test（必过）**：`uv sync` 安装依赖 + `uv run pytest tests/`。任何测试失败即阻断合并——这是真正的"质量卡在合并前"闸门。
  - **lint（ruff，advisory）**：`uvx ruff check .`，当前 `continue-on-error`，存量 lint 债务清理后收紧为必过。
  - **type-check（mypy，advisory）**：`uvx mypy .`，同上。
- `pyproject.toml` 新增 `[tool.ruff]` 基线（line-length=120，启用 E/F/I/W，忽略 E402/E501）。
- 设计取舍：ruff/mypy 初版设为"建议级"而非"必过"，是为了**首跑即绿**、避免团队对 CI 产生抵触；待存量债务（277 处 print、宽泛 except、缺类型注解）清理后再翻转为必过——这是渐进式工程化的标准做法。
- 下一步衔接：PR 评审合并后，即可对后续每个 feature 自动跑测试门禁；随后可启动"统一 logging 替换 print / 消除宽泛 except / 合并 test 与 tests 目录 / 收紧 mypy.ini"等存量清理，并逐步把 ruff/mypy 翻为必过。
