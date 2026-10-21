## 🚀 Bot-hosting 自动续期（Cloudflare Workers + GitHub Actions）

这是一个基于 GitHub Actions 的自动化脚本，用于登录并自动续期 [Bot-hosting](https://bot-hosting.net) 服务。

工作流支持 Cloudflare Worker 定时触发、Worker URL 手动触发和 GitHub Actions 页面手动运行。

⚠️ 有cf盾,太垃圾的机房节点可能过不了，建议用稍微干净点的节点,[B2proxy住宅代理](https://www.b2proxy.com/signup?code=0F5133)

━━━━━━━━━━━━━━━━━━━━━━

### 🔐 Secrets 配置说明

| Secret 名称         | 是否必填 | 说明                                              |
|---------------------|----------|---------------------------------------------------|
| EMAIL              | 单账号必填 | Bot-hosting 登录邮箱，也用于续期通知                  |
| BOT_HOSTING_PASSWORD | 单账号必填 | Bot-hosting 登录密码                                 |
| BOT_HOSTING_ACCOUNTS | ❌ 可选 | 多账号 JSON 配置；设置后优先于单账号配置               |
| TG_BOT_TOKEN       | ❌ 可选  | Telegram Bot Token（用于发送通知）                      |
| TG_CHAT_ID         | ❌ 可选  | Telegram Chat ID（接收通知的用户或群组 ID）               |
| CLOUDFLARE_ACCOUNT_ID | ❌ 可选 | Cloudflare Account ID；用于续期成功后调整 Worker Cron      |
| CLOUDFLARE_API_TOKEN  | ❌ 可选 | Cloudflare API Token；需要目标账户的 `Workers Scripts Write` 权限 |
| CLOUDFLARE_WORKER_NAME | ❌ 可选 | Cloudflare Workers & Pages 中部署的 Worker 名称            |

配置这三个 Cloudflare Secrets 后，续期成功会把 Worker Cron 更新到当前时刻约 5 分钟后（向上取整到分钟，因此约 5–6 分钟后触发）。Cloudflare Cron 按 UTC、分钟粒度运行；该计划之后每天在同一时刻重复。未配置这些 Secrets 时，续期仍会正常完成，但不会调整定时任务。

━━━━━━━━━━━━━━━━━━━━━━

## 部署步骤
1：fork 本项目，在actions菜单允许工作流

2：在 `Settings` ➡ `Secrets and variables` ➡ `Actions` 中添加单账号所需的 `EMAIL`、`BOT_HOSTING_PASSWORD`，或按下文多账号格式添加 `BOT_HOSTING_ACCOUNTS`。旧的 `DISCORD_TOKEN` 不再使用。

3：去 Cloudflare Workers 创建一个 Worker，把本项目的 [`workers.js`](workers.js) 内容复制进去并部署，并为它添加一个初始 Cron Trigger。此 Worker 使用一个 Cron Trigger，续期成功后会用新时间替换该计划。

4：在 Cloudflare Worker 的 Variables / Secrets 里添加以下变量

| 变量名称         | 是否必填 | 说明 |
|------------------|----------|------|
| GH_PAT           | ✅ 必填  | GitHub Personal Access Token，用于触发 repository_dispatch |
| GH_USER          | ✅ 必填  | GitHub 用户名，例如 `karlxu11` |
| GH_REPO          | ✅ 必填  | 当前仓库名，例如 `Auto-Renew-Bothosting` |
| AUTH_KEY         | ✅ 必填  | 手动触发密钥，访问 `https://你的Worker域名/?key=AUTH_KEY` 时使用 |
| GH_EVENT_TYPE    | ❌ 可选  | 默认 `cf_timer`，需和 workflow 中 `repository_dispatch.types` 一致 |
| TG_BOT_TOKEN     | ❌ 可选  | Telegram Bot Token，Worker 触发 GitHub Action 后通知 |
| TG_CHAT_ID       | ❌ 可选  | Telegram Chat ID |

## VPNGate SOCKS5 代理

工作流会在 GitHub Actions 的 Ubuntu runner 中临时启动
[vpngate-to-socks](https://github.com/shenyanshu/vpngate-to-socks)，自动连接 VPNGate
节点并把 `socks5://127.0.0.1:10080` 提供给浏览器和 API 请求使用。

- 默认只保留日本节点；
- 默认排除节点名称中包含 `public` 的节点；
- 自动探测 Bot-hosting 连通性，失败后自动切换节点；
- 不再使用 `NODE_LINK` 或远程 sing-box 脚本。

筛选条件可在 `.github/workflows/renew.yml` 的“启动 VPNGate SOCKS5 代理”步骤中修改：

- `VPNGATE_COUNTRY`：国家代码，默认 `JP`；
- `VPNGATE_EXCLUDE_KEYWORD`：节点名称排除关键词，默认 `public`。

该方案依赖 GitHub Actions runner 的 Docker、`privileged` 和 `NET_ADMIN` 网络能力。
SOCKS5 端口只绑定到 `127.0.0.1`，不要改成公网监听。

5：去 Actions 菜单手动运行一次，或访问 `https://你的Worker域名/?key=AUTH_KEY` 测试 Cloudflare 触发 GitHub Action。Cloudflare Cron 首次按上一步设置的计划运行；每次续期成功后，脚本会把它调整为约 5 分钟后的北京时间，并继续每天在该时刻运行。

脚本使用邮箱和密码登录；多账号配置见下文。

### 多账号续期
在 GitHub Actions Secrets 中添加 `BOT_HOSTING_ACCOUNTS`，值为 JSON 数组。每个账号包含 `email`、`password`；`name` 可选：

```json
[
  {"name":"账号 1","email":"first@example.com","password":"密码一"},
  {"name":"账号 2","email":"second@example.com","password":"密码二"}
]
```

账号会按数组顺序逐个使用独立浏览器会话登录和续期。设置 `BOT_HOSTING_ACCOUNTS` 后会忽略单账号的 `EMAIL` 和 `BOT_HOSTING_PASSWORD`。

## 注意事项
* 必填变量必须要填写
* VPNGate 免费节点质量和存活时间不稳定，无法保证每次都能通过 Bot-hosting 的 Cloudflare 验证
* 邮箱密码登录遇到 Cloudflare 验证时会等待验证通过；未通过时本次登录会失败
* 续期失败时会重新打开账单页自动重跑 1 次，最多执行 2 次续期尝试
* 请遵守 Bot-hosting 服务条款，合理使用自动续期
* Cloudflare Worker 的定时任务只会在续期成功后调整；续期失败或未到续期时间时会保留现有计划

## ⚠️ 免责声明
* 本程序仅供学习了解, 非盈利目的，如转载须注明来源。
* 使用本程序必循遵守部署服务器所在地、所在国家和用户所在国家的法律法规, 程序作者不对使用者任何不当行为负责。
