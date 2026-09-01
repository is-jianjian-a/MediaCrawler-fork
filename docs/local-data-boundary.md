# 本机数据与运行状态

MediaCrawler 将源码、长期数据资产和应用运行状态分开管理。

| 内容 | 默认位置 | 管理方式 |
| --- | --- | --- |
| 源码、测试、文档 | 本 Git 仓库 | Git 管理 |
| 采集内容库、导出文件、历史内容库 | `~/data/datasets/mediacrawler/` | 长期数据；不进入 Git；Agent 默认只读 |
| 任务、账号路由、限流状态 | `~/Library/Application Support/MediaCrawler/database/` | 应用运行状态；不进入 Git |
| 隔离浏览器登录 Profile | `~/Library/Application Support/MediaCrawler/browser-data/` | 敏感运行状态；不进入 Git |
| 运行日志 | `~/Library/Logs/MediaCrawler/` | 可轮转运行记录；不进入 Git |

可通过以下环境变量覆盖默认位置：

- `MEDIACRAWLER_DATA_ROOT`
- `MEDIACRAWLER_STATE_ROOT`
- `MEDIACRAWLER_LOG_ROOT`
- `MEDIACRAWLER_SAVE_DATA_PATH`
- `MEDIACRAWLER_SQLITE_DB_PATH`

不要把实际数据库、登录 Profile 或日志复制回源码目录。公开仓库只能包含可复现的程序、schema、测试与说明。
