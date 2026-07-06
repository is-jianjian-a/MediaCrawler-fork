# 数据库迁移报告

**迁移日期**: 2026-05-26  
**迁移时间**: 13:01:28  
**数据库**: sqlite_tables.db  

---

## 1. 备份信息

| 备份文件 | 大小 | 创建时间 |
|---------|------|---------|
| sqlite_tables.db.backup_20260526_130128 | 14.39 MB | 2026-05-26 13:01:28 |
| sqlite_tables.db.backup_20260513 | 3.69 MB | 2026-05-13 (历史备份) |

**状态**: ✅ 备份已完成

---

## 2. 迁移内容

### 2.1 xhs_note 表

| 变更项 | 原类型 | 新类型 | 说明 |
|-------|-------|-------|------|
| liked_count | TEXT | BIGINT | 点赞数 |
| collected_count | TEXT | BIGINT | 收藏数 |
| comment_count | TEXT | BIGINT | 评论数 |
| share_count | TEXT | BIGINT | 分享数 |
| raw_data | (新增) | TEXT | API原始响应JSON |

**迁移记录数**: 4,126 条  
**状态**: ✅ 迁移成功

### 2.2 xhs_note_comment 表

| 变更项 | 原类型 | 新类型 | 说明 |
|-------|-------|-------|------|
| like_count | TEXT | BIGINT | 点赞数 |
| raw_data | (新增) | TEXT | API原始响应JSON |

**迁移记录数**: 7,417 条  
**状态**: ✅ 迁移成功

### 2.3 xhs_creator 表

| 变更项 | 原类型 | 新类型 | 说明 |
|-------|-------|-------|------|
| follows | TEXT | BIGINT | 关注数 |
| fans | TEXT | BIGINT | 粉丝数 |
| interaction | TEXT | BIGINT | 互动数 |

**迁移记录数**: 0 条  
**状态**: ✅ 迁移成功

---

## 3. 验证结果

### 3.1 数据完整性
- ✅ 所有记录数量正确
- ✅ 数字字段已正确转换为整数
- ✅ 新增 raw_data 字段已创建且为空

### 3.2 索引重建
- ✅ xhs_note(note_id) 索引
- ✅ xhs_note(time) 索引
- ✅ xhs_note_comment(comment_id) 索引
- ✅ xhs_note_comment(create_time) 索引

---

## 4. 迁移脚本

迁移脚本位置: `tools/migrate_xhs_db.py`  
验证脚本位置: `tools/verify_migration.py`

---

## 5. 回滚方案

如需要回滚，请执行:
```bash
cd /Users/zhijian/workspace/MediaCrawler
cp database/sqlite_tables.db.backup_20260526_130128 database/sqlite_tables.db
```

---

**总体状态**: ✅ 迁移成功完成

---

## 6. 多账号独立库合并为主库（新增 crawler_account 字段）

**变更日期**: 2026-06-30
**相关分支**: `feature/merge-account-db`
**数据库**: `database/sqlite_tables.db`（主库，固定路径）

### 6.1 背景

原先每个小红书账号使用独立 SQLite 库（`xhs_account_02.db` / `xhs_account_03.db` 等），
抓取时由 `config/db_config.py` 的 `_ACCOUNT_DB_MAP` 映射到对应库。该方式导致：
- 切换账号需切换数据库文件，统计/导出需跨库合并；
- 无法在同一库中按账号区分数据来源。

本次重构将各账号库**合并至统一主库** `sqlite_tables.db`，并通过新增的
`crawler_account` 字段标记每条数据的来源账号。

### 6.2 表结构变更

| 表 | 字段 | 类型 | 默认值 | 说明 |
|----|------|------|--------|------|
| `xhs_note` | `crawler_account` | VARCHAR(64) | `'default'` | 来源账号标识，带索引 |
| `xhs_note_comment` | `crawler_account` | VARCHAR(64) | `'default'` | 来源账号标识，带索引 |

### 6.3 旧账号库处理

- 原独立账号库已**归档**至 `database/.archive/`（如 `sqlite_tables.db.bak_20260630_*`），
  **未删除**，可随时回滚。
- `config/db_config.py` 已移除 `_ACCOUNT_DB_MAP`，`SQLITE_DB_PATH` 固定指向主库；
  当前账号由环境变量 `MEDIACRAWLER_ACCOUNT` 控制，通过 `get_current_account()` 读取。

### 6.4 写入逻辑

`store/xhs/_store_impl.py` 中 `add_content` / `add_comment` 取值规则：

```python
crawler_account = content_item.get("crawler_account") or get_current_account()
```

即：抓取流程显式传入账号时优先使用传入值，否则回退到当前运行账号。

### 6.5 数据迁移验证结果（合并后实测）

| 表 | crawler_account 取值分布 | 总行数 |
|----|--------------------------|--------|
| `xhs_note` | `01`:5494 / `02`:2062 / `03`:795 / `default`:156 | 8507 |
| `xhs_note_comment` | `02`:49115 / `01`:46298 / `default`:707 | 96120 |

结论：历史行已带**真实账号值**（`01/02/03`），仅少量未归类的行取默认 `'default'`，
字段生效、能力可用。

### 6.6 回归防护

新增测试 `tests/test_xhs_crawler_account.py`，覆盖：
- `get_current_account()` 在设/未设 `MEDIACRAWLER_ACCOUNT` 时的返回值；
- `XhsNote` / `XhsNoteComment` 模型声明 `crawler_account` 列；
- 基于独立临时 SQLite 会话真实写入，验证 `crawler_account` 取值规则与 `update` 不覆盖该字段。

### 6.7 回滚方案

如需回到独立账号库模式，从 `database/.archive/` 取回对应库文件，并还原
`config/db_config.py` 的 `_ACCOUNT_DB_MAP` 逻辑（参见 `feature/merge-account-db` 合并前历史）。
