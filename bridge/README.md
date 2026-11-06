# 国服本地代理 v1.0

让 AstrBot 联盟助手插件可以查询国服（腾讯）的召唤师和战绩。代理需要运行在一台登录着国服客户端的电脑上，只用到 Python 3.9+ 标准库。

## 使用

1. 启动并登录国服英雄联盟客户端，进入主界面。
2. 双击 `start_bridge.bat`，或运行 `python cn_bridge.py`。
3. 第一次运行会生成 `bridge.json`，控制台会打印 Token。把地址和 Token 填进插件配置 `cn.bridge_url` / `cn.bridge_token`。
4. 运行 `python cn_bridge.py --check` 可以只检测客户端，不启动服务。

## 原理（参考 League Akari）

- 连接客户端：先通过注册表 `HKCU\Software\Tencent\LOL\InstallPath` 找到安装目录，读取 `LeagueClient/lockfile`；找不到时读取 `LeagueClientUx.exe` 的命令行参数 `--app-port` / `--remoting-auth-token` / `--rso_platform_id`（需要管理员权限）。
- 令牌：`/entitlements/v1/token` 用于战绩（match-history-query），`/lol-league-session/v1/league-session-token` 用于召唤师和段位（summoner-ledge / leagues-ledge）。
- SGP 地址与 League Akari `builtin.ts` 中的配置一致，例如艾欧尼亚是 `https://hn1-k8s-sgp.lol.qq.com:21019`。

## 接口

除 `/health` 外，所有接口都需要 `Authorization: Bearer <token>`。

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

| 键 | 默认 | 说明 |
|---|---|---|
| host / port | 127.0.0.1 / 6198 | AstrBot 在其他机器上时把 host 改为 0.0.0.0 |
| token | 自动生成 | 访问令牌 |
| lockfile / install_dir | 空 | 自动检测失败时手动指定 |
| sgp_servers | {} | 追加或覆盖大区，例如 `{"TENCENT_XX": {"name": "新区", "url": "https://..."}}` |
| sgp_verify_tls | true | 证书校验失败时可以关闭 |
| allow_raw_lcu | false | 是否开放 `/v1/lcu` |

