# 国服远程代理 v1.2

> 💡 AstrBot 和国服客户端在**同一台 Windows 电脑**上时不需要这个代理，插件会直接连接本机客户端。

AstrBot 部署在云服务器、Docker 或 NAS 上时，在登录着国服客户端的电脑上运行本代理，插件通过它查询国服战绩。只用到 Python 3.9+ 标准库。

## 使用

| 步骤 | 操作 |
|:---:|---|
| 1️⃣ | 启动并登录国服客户端，进入主界面 |
| 2️⃣ | 双击 `start_bridge.bat`（或 `python cn_bridge.py`） |
| 3️⃣ | 复制窗口里显示的完整地址，例如 `http://192.168.1.5:6198?token=xxxx` |
| 4️⃣ | 粘贴到插件配置「国服 → 远程国服代理」，然后发送 `/lol 国服状态` 检查 |

`python cn_bridge.py --check` 只检测客户端，不启动服务。

> [!WARNING]
> 代理默认监听 `0.0.0.0`，并用自动生成的 token 鉴权。请**仅在局域网内使用**：不要把端口映射到公网，也不要把带 token 的地址发给他人。
> AstrBot 与游戏电脑不在同一局域网时，建议用 Tailscale、ZeroTier 等虚拟局域网连接，避免直接暴露到公网。

## 原理（参考 League Akari）

- 连接客户端：通过注册表 `HKCU\Software\Tencent\LOL\InstallPath` 找到安装目录读取 `LeagueClient/lockfile`；找不到时读取 `LeagueClientUx.exe` 的命令行参数（需要管理员权限）。
- 令牌：`/entitlements/v1/token` 用于战绩，`/lol-league-session/v1/league-session-token` 用于召唤师和段位。
- SGP 地址与 League Akari `builtin.ts` 一致，例如艾欧尼亚是 `https://hn1-k8s-sgp.lol.qq.com:21019`。

## 接口

除 `/health` 外都需要 `Authorization: Bearer <token>`（插件会从地址里的 `?token=` 自动读取）。

| 路径 | 说明 |
|---|---|
| `/v1/status` | 客户端连接状态、当前大区、登录账号、令牌状态 |
| `/v1/servers` | 大区列表 |
| `/v1/summoner?name=&tag=` | 按 Riot ID 查询（经过当前客户端） |
| `/v1/summoner?puuid=&server=` | 按 PUUID 查询，可以跨区 |
| `/v1/ranked?puuid=&server=` | 段位，只能查当前大区 |
| `/v1/matches?puuid=&server=&start=&count=` | 战绩，可以跨区（SGP 失败时退回 LCU） |
| `/v1/game?game_id=&server=` | 单局详情 |
| `/v1/lcu?path=` | 透传 LCU GET 请求，默认关闭（`allow_raw_lcu`） |

## 配置 bridge.json

首次运行自动生成，一般不用改。

| 键 | 默认 | 说明 |
|---|---|---|
| host / port | 0.0.0.0 / 6198 | 只给本机用可改为 127.0.0.1 |
| token | 自动生成 | 访问令牌，已包含在显示的地址里 |
| lockfile / install_dir | 空 | 自动检测失败时手动指定 |
| sgp_servers | {} | 追加或覆盖大区，例如 `{"TENCENT_XX": {"name": "新区", "url": "https://..."}}` |
| sgp_verify_tls | true | 证书校验失败时可以关闭 |
| allow_raw_lcu | false | 是否开放 `/v1/lcu` |
