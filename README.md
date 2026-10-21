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
| BOT_HOSTING_ACCOUNTS | 多账号必填 | 多账号 JSON 配置；设置后优先于单账号配置               |
| SCHEDULER_URL | ✅ 必填 | Cloudflare Worker 的完整 URL，例如 `https://renew.example.workers.dev` |
| SCHEDULER_AUTH_KEY | ✅ 必填 | 回写账号调度状态的密钥，需与 Worker 中的同名 Secret 一致 |
| TG_BOT_TOKEN       | ❌ 可选  | Telegram Bot Token（用于发送通知）                      |
| TG_CHAT_ID         | ❌ 可选  | Telegram Chat ID（接收通知的用户或群组 ID）               |

━━━━━━━━━━━━━━━━━━━━━━

## 部署步骤
1：fork 本项目，在actions菜单允许工作流

2：在 `Settings` ➡ `Secrets and variables` ➡ `Actions` 中添加单账号所需的 `EMAIL`、`BOT_HOSTING_PASSWORD`，或按下文多账号格式添加 `BOT_HOSTING_ACCOUNTS`。旧的 `DISCORD_TOKEN` 不再使用。

3：去 Cloudflare Workers 创建一个 Worker，把本项目的 [`workers.js`](workers.js) 内容复制进去并部署。创建一个 Workers KV namespace，并在 Worker 的 Settings → Bindings 中添加 KV Namespace binding，变量名设为 `RENEWAL_STATE`（参见 [Cloudflare KV binding 文档](https://developers.cloudflare.com/kv/concepts/kv-bindings/)）。将现有 Cron Trigger 设为 `0 * * * *`，让 Worker 每小时检查一次账号到期状态；没有账号到期时不会触发 GitHub Actions。若之前有多个 Cron Trigger，保留一个即可。

4：在 Cloudflare Worker 的 Variables / Secrets 里添加以下变量，并将 `SCHEDULER_AUTH_KEY` 同样添加到 GitHub Actions Secrets：

| 变量名称         | 是否必填 | 说明 |
|------------------|----------|------|
| GH_PAT           | ✅ 必填  | GitHub Personal Access Token，用于触发 repository_dispatch |
| GH_USER          | ✅ 必填  | GitHub 用户名，例如 `karlxu11` |
| GH_REPO          | ✅ 必填  | 当前仓库名，例如 `Auto-Renew-Bothosting` |
| AUTH_KEY         | ✅ 必填  | 手动触发密钥，访问 `https://你的Worker域名/?key=AUTH_KEY` 时使用 |
| SCHEDULER_AUTH_KEY | ✅ 必填 | Worker 接收续期状态回写的密钥；需与 GitHub Actions Secret 同值 |
| BOT_HOSTING_ACCOUNT_IDS | ✅ 必填 | 账号 ID，逗号分隔，顺序和多账号配置一致，例如 `account_1,account_2,account_3` |
| GH_EVENT_TYPE    | ❌ 可选  | 默认 `cf_timer`，需和 workflow 中 `repository_dispatch.types` 一致 |
| TG_BOT_TOKEN     | ❌ 可选  | Telegram Bot Token，Worker 触发 GitHub Action 后通知 |
| TG_CHAT_ID       | ❌ 可选  | Telegram Chat ID |

GitHub Actions Secrets 中还需添加 `SCHEDULER_URL`（Worker 的完整 URL）和同值的 `SCHEDULER_AUTH_KEY`。`RENEWAL_STATE` KV 中只保存账号 ID、状态和下次检查时间，不保存邮箱或密码。

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

5：完成账号 ID、KV binding 和新增 Secrets 配置后，去 Actions 菜单手动运行一次，或访问 `https://你的Worker域名/?key=AUTH_KEY` 初始化账号时间。KV 中没有记录的账号会在首次 Worker 定时检查时一并处理；之后 Worker 每小时只触发已到期账号，触发时间最多比账号可续期时间晚约一小时。手动 URL 可加 `&account=account_1` 来触发单个账号。

脚本使用邮箱和密码登录；多账号配置见下文。

### 多账号续期
在 GitHub Actions Secrets 中添加 `BOT_HOSTING_ACCOUNTS`，值为 JSON 数组。每个账号包含稳定且唯一的 `id`、`email`、`password`；`name` 可选。每个 `id` 必须和 Worker 的 `BOT_HOSTING_ACCOUNT_IDS` 对应：

```json
[
  {"id":"account_1","name":"账号 1","email":"first@example.com","password":"密码一"},
  {"id":"account_2","name":"账号 2","email":"second@example.com","password":"密码二"},
  {"id":"account_3","name":"账号 3","email":"third@example.com","password":"密码三"}
]
```

账号会按数组顺序逐个使用独立浏览器会话登录和续期。脚本将页面倒计时换算成各自的下次检查时间，并回写 Worker；失败账号会延后 6 小时重试。单账号模式默认使用 ID `account_1`。设置 `BOT_HOSTING_ACCOUNTS` 后会忽略单账号的 `EMAIL` 和 `BOT_HOSTING_PASSWORD`。

## 注意事项
* 必填变量必须要填写
* VPNGate 免费节点质量和存活时间不稳定，无法保证每次都能通过 Bot-hosting 的 Cloudflare 验证
* 邮箱密码填写完成后等待 10 秒，再点击 Cloudflare 验证复选框；最多尝试 3 次，确认验证通过后才点击 Sign in，未通过时本次登录会失败
* 登录步骤截图仅保存在运行环境本地，不发送 Telegram；续期结果通知和截图仍按 Telegram 配置发送
* 续期失败时会重新打开账单页自动重跑 1 次，最多执行 2 次续期尝试
* 请遵守 Bot-hosting 服务条款，合理使用自动续期
* Worker 每小时检查一次 KV；未到时间的账号不会启动浏览器或 GitHub Actions

### 常见问题
- `回写账号 ... 调度时间失败: HTTP 401`：检查 Cloudflare Worker 和 GitHub Actions 的 `SCHEDULER_AUTH_KEY` 是否完全一致；Cloudflare 端应设置为 Worker Secret，更新后重新部署 Worker。不要把值发到日志或聊天中。
- 登录停留在 `/login`：新版日志会打印页面可见的错误提示（若网站提供），以及密码框是否仍显示。先按提示检查邮箱和密码；若没有提示，再从本次运行的 `renew-log` artifact 查看完整日志。

## ⚠️ 免责声明
* 本程序仅供学习了解, 非盈利目的，如转载须注明来源。
* 使用本程序必循遵守部署服务器所在地、所在国家和用户所在国家的法律法规, 程序作者不对使用者任何不当行为负责。
