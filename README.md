# AnyRouter 自动签到（GitHub Actions 版）

只跑在 GitHub Actions 上的多账号自动签到，兼容 NewAPI / OneAPI 平台，内置 `anyrouter` 与 `agentrouter`。

每次运行的流程：

1. 🛡️ 安装 sing-box，按 `NODE_LINK` 启动本地代理，检测出口 IP（打码显示，如 `122.122.*.*`）
2. 🚀 逐个账号签到（邮箱密码或 cookies）
3. 📣 有失败或余额变化时推送通知

> **代理是必需的**：代理任何一步失败（下载、解析、启动、连通性、出口 IP），workflow 立即失败，**不会执行签到**，也不会回退直连。

## 快速开始

1. Fork 本仓库。
2. 进入 `Settings → Environments → New environment`，新建名为 `production` 的环境。
3. 在该环境的 **Environment secrets** 中添加下表的 Secret。
4. 到 `Actions` 页启用 workflow，手动点一次 `Run workflow` 测试（可勾选 debug 获取截图）。默认每 6 小时运行一次。

## Secrets

| 名称 | 必填 | 说明 |
| --- | --- | --- |
| `NODE_LINK` | ✅ | 节点分享链接，见下方「支持的节点协议」 |
| `ANYROUTER_ACCOUNTS` | ✅ | 账号配置，单行 JSON 数组，见下方「账号配置」 |
| `PROVIDERS` | 否 | 自定义平台配置，见下方「自定义平台」 |
| 通知类 | 否 | 见下方「通知」 |

> 节点链接含密码，只放在 Secret 里，不要提交到仓库。

## 支持的节点协议

| 协议 | 链接示例 | 说明 |
| --- | --- | --- |
| Hysteria2 | `hysteria2://密码@host:443?sni=a.com&insecure=1#名称` | 支持 `hy2://`、obfs；**不支持 `pinSHA256`**（会被忽略） |
| VLESS | `vless://uuid@host:443?security=reality&pbk=...&sid=...&flow=...#名称` | 支持 tls / reality、flow |
| VMess | `vmess://base64(JSON)` | 常见 v2rayN 分享格式 |
| Trojan | `trojan://密码@host:443?sni=a.com#名称` | 默认启用 TLS |
| Shadowsocks | `ss://base64(method:password)@host:8388#名称` | 新旧两种格式；不支持 plugin |
| TUIC | `tuic://uuid:密码@host:443?congestion_control=bbr&sni=a.com#名称` | v5 |

传输方式支持：`tcp`、`ws`、`grpc`、`http`(h2)、`httpupgrade`。`xhttp`、`kcp`、`quic` 等暂不支持，会在日志里明确报错。

## 账号配置

`ANYROUTER_ACCOUNTS` 为单行 JSON 数组，每个账号二选一：

```json
[
  {
    "name": "主账号",
    "provider": "anyrouter",
    "email": "your@email.com",
    "password": "your_password"
  },
  {
    "name": "备用账号",
    "provider": "agentrouter",
    "cookies": { "session": "abc123session" },
    "api_user": "user123"
  }
]
```

- `provider` 不填时默认 `anyrouter`；`name` 不填时显示为 `Account N`。
- 邮箱密码登录优先；失败不会回退到过期 cookies。
- 使用 `cookies` 时必须同时提供 `api_user`（请求头 `new-api-user`）。获取方式：登录站点后在浏览器开发者工具中复制 cookies 与用户 ID。

## 代理说明

- 内置的 `anyrouter`、`agentrouter` 默认都 `use_proxy: true`。
- 代理只对开启 `use_proxy` 的平台生效，不是全局代理。要让某个平台直连，在 `PROVIDERS` 中设置 `{"anyrouter":{"use_proxy":false}}`；注意 workflow 的代理步骤始终要求 `NODE_LINK` 有效。
- 可通过 workflow 里的 `SINGBOX_VERSION` 指定 sing-box 版本，`PROXY_TEST_URL` 修改连通性探测地址。

## 自定义平台（可选）

内置平台无需配置。接入其他 NewAPI 站点时，添加 Secret `PROVIDERS`（单行 JSON）：

```json
{
  "customrouter": {
    "domain": "https://custom.example.com",
    "login_path": "/login",
    "sign_in_path": "/api/user/sign_in",
    "user_info_path": "/api/user/self",
    "api_user_key": "new-api-user",
    "bypass_method": "waf_cookies",
    "waf_cookie_names": ["acw_tc", "cdn_sec_tc", "acw_sc__v2"],
    "use_proxy": true
  }
}
```

只有 `domain` 必填，其余字段都有默认值；同名配置会覆盖内置配置。`sign_in_path` 设为 `null` 表示查询用户信息时自动签到。`bypass_method` 为 `"waf_cookies"` 时会用 CloakBrowser 获取 WAF cookies。

## 通知

只有失败或余额变化时才推送；未配置的渠道会自动跳过。在 Environment secrets 中按需添加：

| 渠道 | Secrets |
| --- | --- |
| 邮箱 | `EMAIL_USER`、`EMAIL_PASS`、`EMAIL_TO`，可选 `EMAIL_SENDER`、`CUSTOM_SMTP_SERVER` |
| 钉钉 | `DINGDING_WEBHOOK`（机器人安全设置选"自定义关键词"时填 `AnyRouter`） |
| 飞书 | `FEISHU_WEBHOOK` |
| 企业微信 | `WEIXIN_WEBHOOK` |
| PushPlus | `PUSHPLUS_TOKEN` |
| Server 酱 | `SERVERPUSHKEY` |
| Telegram | `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` |
| Gotify | `GOTIFY_URL`、`GOTIFY_TOKEN`，可选 `GOTIFY_PRIORITY` |
| Bark | `BARK_KEY`，可选 `BARK_SERVER` |

## 故障排除

- **代理步骤失败**：看日志里的 ❌ 原因。常见：`NODE_LINK` 未配置或格式不对；节点离线；hysteria2 / tuic 需要服务器放行 UDP 端口；密码 / UUID / SNI 填错。
- **登录失败 / 401**：邮箱密码是否正确；cookies 是否过期（重新获取）；`api_user` 是否正确。
- **WAF 拦截**：开启 debug 运行，下载 `checkin-screenshots-*` artifact 查看截图。
- 支持部分账号失败：只要有账号签到成功，整个任务就不会标记失败。

## 免责声明

本脚本仅用于学习和研究目的，使用者需自行承担使用风险，并遵守相关网站的服务条款。
