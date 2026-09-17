## 🚀 Bot-hosting 自动续期（Cloudflare Workers + GitHub Actions）

这是一个基于 GitHub Actions 的自动化脚本，用于登录并自动续期 [Bot-hosting](https://bot-hosting.net) 服务。

工作流支持 GitHub 每日定时触发、Cloudflare Worker 触发和手动触发。

⚠️ 有cf盾,太垃圾的机房节点可能过不了，建议用稍微干净点的节点,[B2proxy住宅代理](https://www.b2proxy.com/signup?code=0F5133)

━━━━━━━━━━━━━━━━━━━━━━

### 🔐 Secrets 配置说明

| Secret 名称         | 是否必填 | 说明                                              |
|---------------------|----------|---------------------------------------------------|
| EMAIL              | ❌ 可选  | 用于通知使用的Email,可随意填写                          |
| SESSION_TOKEN      | ❌ 可选  | Bot-hosting session_token，cookie里获取               |
| DISCORD_TOKEN      | ✅ 必填  | Discord Token，SESSION_TOKEN失效时自动OAuth登录        |
| GH_TOKEN           | ❌ 可选  | GitHub(classic) token,用于自动更新session_token,以ghp_xxx开头|
| TG_BOT_TOKEN       | ❌ 可选  | Telegram Bot Token（用于发送通知）                      |
| TG_CHAT_ID         | ❌ 可选  | Telegram Chat ID（接收通知的用户或群组 ID）               |

━━━━━━━━━━━━━━━━━━━━━━

## 部署步骤
1：fork 本项目，在actions菜单允许工作流

2：在`setting`➡`secrets and variables`➡`Actions` 里添加上方必填的secrets

3：去 Cloudflare Workers 创建一个 Worker，把本项目的 [`workers.js`](workers.js) 内容复制进去并部署。

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

5：在 GitHub 仓库的 `Settings → Secrets and variables → Actions` 添加以下 Secrets，使每次运行后能自动设置下一次 Cloudflare 定时任务：

| Secret 名称 | 说明 |
|---|---|
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare 账户 ID |
| `CLOUDFLARE_WORKER_NAME` | 已部署 Worker 的名称 |
| `CLOUDFLARE_API_TOKEN` | 创建一个仅限该账户、权限为 `Workers Scripts: Edit` 的 API Token |

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

脚本读取到账单页的到期日后，会把该 Worker 的唯一 Cron 设置为**该日期 23:00（北京时间）**。例如到期日是 `2026/08/27`，将设置为 `2026/08/27 23:00`。未读到到期日或 Cloudflare 凭据未配置时，会保留原有定时任务。

6：去 Actions 菜单手动运行一次，或访问 `https://你的Worker域名/?key=AUTH_KEY` 测试 Cloudflare 触发 GitHub Action。GitHub 定时任务每天 UTC 00:02（北京时间 08:02）自动运行。

### SESSION_TOKEN 获取
登录你的账号,按F12或页面空白处 右键➡检查➡选择应用程序或appcations 找到对应的字段点击获取对应的值，详情如图
<img width="1200" height="600" alt="image" src="https://github.com/user-attachments/assets/e532b0d6-9f12-45fd-8af9-69e1029a1a92" />

### DISCORD_TOKEN 获取（用于 SESSION_TOKEN 失效后备用登录
1. 浏览器登录 Discord（网页版）
2. 按 F12 打开开发者工具 ➡ 网络 ➡ 点击任意频道 ➡ 选择左侧的任意api 
3. 找到名为 `authorization字段` 的 值，即为discord token,详情如图所示
<img width="1200" height="600" alt="image" src="https://github.com/user-attachments/assets/7276d62d-31ff-452c-9e13-165af8323f53" />


> **作用**：当 `SESSION_TOKEN` 过期导致登录失败时，脚本会自动使用 Discord Token 走 OAuth 流程重新登录，并自动更新 `SESSION_TOKEN` Secret，实现永久免维护。


### 获取 `GH_TOKEN`(GitHub Personal Access Token)
1：点击GitHub 账户右上角头像 → Settings（设置）。

2：左侧菜单底部点击 Developer settings（开发者设置）。

3：点击 Personal access tokens → Tokens (classic)。

4：点击 Generate new token → Generate new token (classic)。

填写信息：
- Note：起一个描述性名称（如 my-token）。
- Expiration：选择过期时间（建议选No expiration永不过期）。
- Select scopes：勾选所需权限（不知道如何勾选就全部勾选）。
- 点击 Generate token，立即复制并妥保存生成的 token（离开页面后不能再查看）。

## 注意事项
* 必填变量必须要填写
* VPNGate 免费节点质量和存活时间不稳定，无法保证每次都能通过 Bot-hosting 的 Cloudflare 验证
* 续期失败时会重新打开账单页自动重跑 1 次，最多执行 2 次续期尝试
* 自动续期不代表可以无底线的薅羊毛,不建议多账号
* 脚本每次运行成功后，会按页面到期日自动更新 Cloudflare Worker 的 Cron Trigger；GitHub 每日定时仍然保留

## ⚠️ 免责声明
* 本程序仅供学习了解, 非盈利目的，如转载须注明来源。
* 使用本程序必循遵守部署服务器所在地、所在国家和用户所在国家的法律法规, 程序作者不对使用者任何不当行为负责。
