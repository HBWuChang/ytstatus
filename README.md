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

## 让 AI 协助安装

安装 Skill 位于 `skills/ytstatus-setup/SKILL.md`。在 Codex 中可使用 `$ytstatus-setup`；其他 AI 可直接读取该文件，并按其中流程询问安装位置、协助创建 YouTrack Token、按岗位配置查询及完成 SwiftBar 验收。

## VIPTest 一天会员插件

`plugins/viptest-vip.1m.py` 提供菜单项 **添加一天会员（测试账号）**。依赖 Python 3 标准库，无需 pip 安装。当前 SwiftBar 已使用本仓库 `plugins` 目录时，刷新插件即可出现 `VIP +1`。

配置在仓库根目录 `viptest-config.json`，与 YouTrack 的配置分开。首次安装可复制 `viptest-config.example.json`，并执行 `chmod 600 viptest-config.json`。本次已生成本地配置，填入指定邮箱及 TOTP 信息；只需补全 `password`。

- `username`：Keycloak 登录用户名或邮箱。
- `password`：Keycloak 登录密码。
- `target_email`：要添加会员的账号邮箱，可以与登录账号不同。
- `basic_username` / `basic_password`：VIPTest 网站网关的 HTTP Basic 凭据，仅发送给 `https://www.viptest.co`，不会转发给 Keycloak。
- `totp`：单账号 Google Authenticator 迁移链接、普通 `otpauth://totp/` 链接或 Base32 密钥。多账号迁移链接会拒绝执行。

操作步骤：

1. 点击 **编辑配置**，填写密码并保存。
2. 点击 **检查登录和目标账号**：验证用户名/密码、TOTP、精确邮箱匹配、会员编辑权限和 CSRF，不提交会员修改。
3. 点击 **添加一天会员（测试账号）**：执行一次添加，结果显示在菜单中。

也可在仓库根目录运行：

```sh
python3 plugins/viptest-vip.1m.py --check
python3 plugins/viptest-vip.1m.py --add
```

### 抓包对应关系

依据 2026-10-27 Reqable 当前捕获：

- #2、#7、#13、#19–#23：Censor 登录入口 → Keycloak 密码表单 → OTP 表单 → OIDC 回调建立 Censor Cookie。
- #44、#45：`GET /censor/search/?type=usernameoremail&arg=<邮箱>` 跳转到个人资料。插件从 `email_<用户ID>` 精确确认目标，不使用搜索框回显或固定用户 ID。
- #96：动态读取 `GET /censor/profile/edit?userId=<ID>&type=membership` 表单中的 CSRF、用户名、会员状态。
- #102：POST 同一编辑地址。固定 `Profile[grade]=0`（premium）、`Profile[member_duration]=1`、`Profile[member_type]=1`（天），理由 `Test account / CS account.`；`ret=1` 表示后台返回成功。不会增加金币、Super Like、取消自动续费或设为 guest。

每次操作新建登录会话，Cookie 仅保存在进程内存；密码和 TOTP 留在权限为 600 的本地配置，配置已被 Git 忽略。菜单每分钟刷新只读取本地状态，不访问后台、不增加会员。提交互斥锁避免并发，60 秒内防重复提交。若提交超时、进程中断或响应无法确认，保留“结果待核查”状态并阻止再次提交；先在后台确认是否已到账，再点击 **已核查，解除提交锁**。接口没有已知幂等键，因此不会自动重试提交。

验证：`python3 -m unittest discover -s tests -v`。已用真实抓包表单验证生成的全部提交字段与 #102 一致；已在 2026-10-27 17:13（北京时间）通过插件实际登录并为配置目标添加一次一天会员，后台返回 `ret=1`。服务器成功响应代表请求被接受，最终到期时间仍以后台资料为准。

菜单格式遵循 [SwiftBar 官方插件文档](https://github.com/swiftbar/SwiftBar#creating-plugins)。

诊断日志：添加 `--debug` 可在 stderr 查看执行阶段、请求方法/路径、HTTP 状态、跳转、耗时和异常代码位置。查询参数、密码、OTP、Cookie、请求体及原始响应内容不会输出。例如：

```sh
python3 plugins/viptest-vip.1m.py --add --debug
```

2026-10-27 实际运行在登录入口失败：原插件 User-Agent 得到 HTTP 405 人机验证页；使用浏览器格式请求头后得到 HTTP 401，`WWW-Authenticate` 表示网关要求 HTTP Basic 认证。随后加入用户提供的 HTTP Basic 凭据，完整登录与会员提交成功（后台返回 `ret=1`）。`--debug` 也适用于 `--check`。
