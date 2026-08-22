# YouTrack Status for SwiftBar

状态栏每分钟查询一次 YouTrack，显示符合 `config.json` 中搜索条件的 issue 数量和本人当月排名。点击展开后可逐条打开未完成 issue，也可一次打开全部；菜单下方同时展示月榜前若干名。

未完成 issue 按 `Expiration time` 升序排列，并在编号前显示北京时间；未设置过期时间的 issue 排在最后。字段名和时区可通过 `config.json` 的 `expiration_field`、`timezone` 调整。

## 文件

- `plugins/youtrack-count.1m.py`：SwiftBar 插件
- `config.json`：YouTrack 地址、令牌和搜索条件（权限应为 `600`，且不会被 Git 跟踪）

## 使用

1. 打开 SwiftBar。
2. 首次设置插件目录时选择本目录下的 `plugins` 文件夹。
3. 修改 `config.json` 的 `query` 即可改变搜索条件。
4. `report_id` 指定月榜报表，`ranking_limit` 控制展开菜单显示的人数（1–50）。

脚本文件名中的 `.1m.` 表示每分钟自动刷新。点击菜单中的“立即刷新”可手动刷新。
