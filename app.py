#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sys, time, json, requests, subprocess
import urllib.request, urllib.parse, urllib.error
from datetime import datetime
from seleniumbase import SB

# 环境变量配置(可以直接私库在双引号里填写)
EMAIL         = os.environ.get("EMAIL") or ""           # 邮箱,只用于通知使用，可随意填写
SESSION_TOKEN = os.environ.get("SESSION_TOKEN") or ""   # session token，默认登录方式,非必须
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") or ""   # Discord Token 备用登录方式, 失败时才使用,必须填写
GH_TOKEN      = os.environ.get("GH_TOKEN") or ""        # GitHub PAT token,用于自动更新session token,可选
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID") or ""      # TG chat id,不填写不通知，需和bot token一起填写生效
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN") or ""    # TG bot token 

# 解析 DISCORD_TOKEN
DC_TOKEN = ""
if DISCORD_TOKEN:
    _parts = DISCORD_TOKEN.split(",", 1)
    DC_TOKEN = _parts[-1].strip()

if not SESSION_TOKEN and not DC_TOKEN:
    print("ℹ️ 未配置 SESSION_TOKEN 和 DISCORD_TOKEN,脚本终止。")
    sys.exit(1)

# 构造cookie
COOKIES = {
    "session_token": SESSION_TOKEN,
    "login": "true",
    "theme": "system",
}

# 记录本次登录方式（用于通知）
_LOGIN_METHOD = "SESSION_TOKEN"

# 获取cookie到期时间
def get_cookie_info(sb, name):
    cookies = sb.get_cookies()
    for c in cookies:
        if c.get('name') == name:
            value = c.get('value')
            expiry_ts = c.get('expiry')
            expiry_dt = datetime.fromtimestamp(expiry_ts) if expiry_ts else None
            return value, expiry_dt
    return None, None

# 检查是否需要更新cookie
def should_update_cookie(new_value, old_value, expiry_dt, days_threshold=3):
    if new_value is None:
        return False
    if new_value != old_value:
        return True
    if expiry_dt:
        remaining = (expiry_dt - datetime.now()).total_seconds()
        if remaining < days_threshold * 24 * 3600:
            return True
    return False

# 更新cookie到secrets
def update_github_secret(secret_name, new_value):
    if not new_value:
        print(f"⚠️ 跳过更新 {secret_name}：新值为空")
        return False
    masked = new_value[:4] + "..." + new_value[-4:] if len(new_value) > 8 else "***"
    print(f"🔄 更新 Secret: {secret_name} (新值: {masked})")
    try:
        env = os.environ.copy()
        if GH_TOKEN:
            env["GH_TOKEN"] = GH_TOKEN
        proc = subprocess.run(
            ["gh", "secret", "set", secret_name, "--body", new_value],
            capture_output=True, text=True, timeout=30, check=False,
            env=env
        )
        if proc.returncode == 0:
            return True
        else:
            print(f"❌ 更新失败: {proc.stderr.strip()}")
            return False
    except Exception as e:
        print(f"❌ 异常: {e}")
        return False

# 发送tg通知
def send_telegram_message(message: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，跳过通知")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": message}, timeout=10)
        print("✅ Telegram 通知已发送")
    except Exception as e:
        print(f"❌ Telegram 发送失败: {e}")

# 通知格式
def format_notification(status: str, extra: str = "", error: str = "") -> str:
    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        if len(name) > 4:
            masked_email = f"{name[:2]}****{name[-2:]}@{domain}"
        else:
            masked_email = f"{name}@{domain}"
    else:
        masked_email = EMAIL[:2] + '****' 
    
    lines = [
        "🇫🇮 Bot-hosting 续期通知",
        "",
        f"{status}",
        f"👤 登录账户: {masked_email}",
    ]
    if _LOGIN_METHOD != "SESSION_TOKEN":
        lines.append(f"🔐 登录方式: {_LOGIN_METHOD}")
    if extra:
        lines.append(extra)
    if error:
        lines.append(f"⚠️ 错误信息: {error}")
    lines.append(f"⏱️ 登录时间: {now}")
    return "\n".join(lines)

# 等待Turnstile验证通过
def wait_for_turnstile_pass(sb, timeout=30):
    start = time.time()
    cf_indicators = ["verify you are human", "确认您是真人", "troubleshoot", "just a moment"]
    while time.time() - start < timeout:
        page_lower = sb.get_page_source().lower()
        if not any(x in page_lower for x in cf_indicators):
            print("✅ Turnstile 验证已通过")
            # sb.save_screenshot("turnstile_passed.png")
            return True
        sb.sleep(1)
    print("❌ Turnstile 验证超时未通过")
    return False
    
# 获取当前出口ip
def get_current_ip(proxy_server: str = "") -> str:
    proxies = None
    if proxy_server:
        proxies = {"http": proxy_server, "https": proxy_server}
    response = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
    response.raise_for_status()
    return response.text.strip()

# 时间格式化
def format_countdown(countdown_str: str) -> str:
    try:
        h, m, _ = countdown_str.split(':')
        h = int(h)
        m = int(m)
        if h > 0:
            return f"{h}h{m}min"
        else:
            return f"{m}min"
    except:
        return countdown_str

#   Discord OAuth 登录（SESSION_TOKEN 失效时的备用方案）
DISCORD_CLIENT_ID   = "884382422530158623"
OAUTH_REDIRECT_URI  = "https://bot-hosting.net/login"
OAUTH_SCOPE         = "identify email guilds"
DISCORD_API         = "https://discord.com/api/v9/oauth2/authorize"
OAUTH_LOGIN_REDIRECT = "/a/billings"
DISCORD_LOGIN_ATTEMPTS = 2
DISCORD_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)
STATE_RE = re.compile(r"[?&]state=([^&]+)")


def capture_discord_state(sb, redirect_path: str = OAUTH_LOGIN_REDIRECT) -> str:
    """打开 /login/discord，从落地页 URL 里提取本次会话的 state"""
    print("🔎 获取 Discord OAuth state...")
    login_url = "https://bot-hosting.net/login/discord?" + urllib.parse.urlencode({
        "redirect": redirect_path,
    })
    sb.uc_open_with_reconnect(login_url, reconnect_time=4)
    time.sleep(2)

    url = sb.get_current_url()
    if "discord.com" not in url:
        print(f"⚠️ 未跳转到 Discord 相关页面，当前 URL：{url}")
        return ""

    m = STATE_RE.search(url)
    if not m:
        print(f"❌ 未能从 URL 中解析出 state，当前 URL：{url}")
        return ""

    state = urllib.parse.unquote(m.group(1))
    print(f"✅ 已捕获 state（当前落地页：{urllib.parse.urlparse(url).path}）")
    return state


def discord_authorize(state: str) -> str:
    """用 DC_TOKEN 直接完成 Discord 侧授权，返回跳转回 bot-hosting.net 的 location"""
    query = urllib.parse.urlencode({
        "client_id":     DISCORD_CLIENT_ID,
        "response_type": "code",
        "redirect_uri":  OAUTH_REDIRECT_URI,
        "scope":         OAUTH_SCOPE,
        "state":         state,
    })
    authorize_url = f"{DISCORD_API}?{query}"

    referer = (
        "https://discord.com/oauth2/authorize?" +
        urllib.parse.urlencode({
            "client_id":     DISCORD_CLIENT_ID,
            "redirect_uri":  OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope":         OAUTH_SCOPE,
            "state":         state,
        })
    )

    headers = {
        "accept":           "*/*",
        "authorization":    DC_TOKEN,
        "content-type":     "application/json",
        "origin":           "https://discord.com",
        "referer":          referer,
        "user-agent":       DISCORD_UA,
        "x-discord-locale": "zh-CN",
    }

    body = json.dumps({
        "permissions": "0",
        "authorize": True,
        "integration_type": 0,
        "location_context": {
            "guild_id": "10000",
            "channel_id": "10000",
            "channel_type": 10000,
        },
    })

    # 如果配置了代理，Discord API 请求也走代理
    proxies = None
    _is_proxy = os.environ.get("IS_PROXY", "false").lower() == "true"
    _proxy_server = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
    if _is_proxy:
        proxies = {"http": _proxy_server, "https": _proxy_server}

    try:
        resp = requests.post(authorize_url, headers=headers, data=body, proxies=proxies, timeout=20)
        if resp.status_code != 200:
            print(f"❌ Discord OAuth2 授权失败: HTTP {resp.status_code} - {resp.text[:300]}")
            return ""
        resp_data = resp.json()
    except Exception as e:
        print(f"❌ Discord OAuth2 授权异常: {e}")
        return ""

    location = resp_data.get("location", "")
    if not location:
        print(f"❌ 授权响应中未找到 location 字段: {resp_data}")
        return ""

    masked = re.sub(r"code=[^&]+", "code=***", location)
    print(f"✅ 拿到回调 URL: {masked}")
    return location


def open_discord_callback(sb, location: str, attempt_number: int) -> bool:
    """打开一次性 OAuth 回调，并等待 Bot-hosting 完成登录跳转。"""
    print("↩️ 携带授权码打开回调链接...")
    try:
        # 回调中的 code 只能使用一次；这里不使用 uc_open_with_reconnect，避免
        # 自动重连时重复提交同一个 code，导致服务端返回 disconnect。
        sb.open(location)
        sb.wait_for_ready_state_complete()
    except Exception as e:
        print(f"❌ 打开 OAuth 回调失败: {e}")
        sb.save_screenshot(f"login_callback_error_{attempt_number}.png")
        return False

    try:
        body_text = sb.get_text("body")
    except Exception:
        body_text = ""
    if "fraud" in body_text.lower():
        print("🚫 触发风控（fraud attempt），可能是 IP 被拦截")
        sb.save_screenshot(f"login_fraud_{attempt_number}.png")
        return False

    for _ in range(30):
        url = sb.get_current_url()
        parsed_url = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed_url.query)

        if query.get("error"):
            error = query["error"][0]
            description = query.get("error_description", [""])[0]
            detail = f"{error}: {description}" if description else error
            print(f"❌ Bot-hosting OAuth 回调失败: {detail}，当前 URL：{url}")
            try:
                body_text = sb.get_text("body")
                print(f"📄 页面正文片段：{body_text[:200].strip()!r}")
            except Exception:
                pass
            sb.save_screenshot(f"login_oauth_error_{attempt_number}.png")
            return False

        if parsed_url.hostname in {"bot-hosting.net", "www.bot-hosting.net"}:
            path = parsed_url.path
            if path != "/login" and not path.startswith("/login/"):
                print(f"✅ Discord OAuth 登录成功！当前页面：{url}")
                return True

        time.sleep(0.5)

    print(f"❌ 登录超时或未跳转成功，最终停留在：{url}")
    try:
        body_text = sb.get_text("body")
        print(f"📄 页面正文片段：{body_text[:200].strip()!r}")
    except Exception:
        pass
    sb.save_screenshot(f"login_timeout_{attempt_number}.png")
    return False


def do_discord_login(sb) -> bool:
    """通过 Discord Token 登录，OAuth 回调失败时重新生成 state 并重试一次。"""
    print("\n🔑 通过 Discord Token 登录...")

    for attempt_number in range(1, DISCORD_LOGIN_ATTEMPTS + 1):
        if attempt_number > 1:
            print("🔁 OAuth 回调失败，重新获取 state 和授权码后再试一次...")

        state = capture_discord_state(sb)
        if not state:
            sb.save_screenshot(f"login_no_state_{attempt_number}.png")
            continue

        location = discord_authorize(state)
        if not location:
            continue

        if open_discord_callback(sb, location, attempt_number):
            return True

    print(f"❌ Discord OAuth 登录失败，已尝试 {DISCORD_LOGIN_ATTEMPTS} 次")
    return False


def open_bot_hosting_for_cookies(sb) -> str:
    """打开目标站点并确认当前页面允许写入 Bot-hosting Cookie。"""
    target_url = "https://bot-hosting.net/"
    allowed_hosts = {"bot-hosting.net", "www.bot-hosting.net"}

    for attempt in range(2):
        sb.open(target_url)
        sb.wait_for_ready_state_complete()
        sb.sleep(2)

        current_url = sb.get_current_url()
        current_host = (urllib.parse.urlparse(current_url).hostname or "").lower().rstrip(".")
        print(f"🧭 Cookie 注入前页面: {current_url}")

        if current_host in allowed_hosts:
            return current_host

        if attempt == 0:
            print("⚠️ 当前页面不在 Bot-hosting 域名，重新打开目标站点...")

    raise RuntimeError(
        f"Cookie 注入前页面域名异常，当前 URL: {current_url}；"
        "可能是代理连接失败、站点跳转或 Cloudflare 验证未完成"
    )


def find_renew_control(sb):
    """查找续期按钮或倒计时，返回 (按钮选择器, 倒计时)。"""
    possible_selectors = [
        'button:contains("Renew")',
        'button:contains("Renew free plan")',
        'a:contains("Renew")',
        '[class*="renew"]',
        '[class*="Renew"]',
    ]

    for selector in possible_selectors:
        try:
            if not sb.is_element_visible(selector):
                continue

            button_text = sb.get_text(selector)
            if "Renew in" in button_text:
                match = re.search(r"Renew in (\d{2}:\d{2}:\d{2})", button_text)
                if match:
                    return None, match.group(1)
            elif "Renew" in button_text and "in" not in button_text.lower():
                print(f"✅ 续期按钮可用: '{button_text}'")
                return selector, None
        except Exception:
            # 某个候选选择器失效不应影响后续选择器的检查。
            continue

    return None, None


def open_billings_page(sb) -> bool:
    """重新打开账单页，用于续期失败后的单次重试。"""
    try:
        print("🌐 重新访问 https://bot-hosting.net/a/billings ...")
        sb.open("https://bot-hosting.net/a/billings")
        sb.wait_for_ready_state_complete()
        sb.sleep(3)
        current_url = sb.get_current_url()
        current_title = sb.get_title()
        print(f"📝 重试页面 URL: {current_url}, Title: {current_title}")
        return "a/billings" in current_url and "/login" not in current_url and "error=" not in current_url
    except Exception as e:
        print(f"❌ 重新打开账单页失败: {e}")
        return False


def attempt_renewal(sb, attempt_number: int, previous_attempt=False):
    """执行一次完整续期尝试，返回 success / not_due / failed 结果。"""
    print(f"🔄 开始第 {attempt_number} 次续期尝试")

    outer_renew_selector, countdown_text = find_renew_control(sb)
    if countdown_text:
        extra = f"⏱️ 可续期时间: {format_countdown(countdown_text)}后"
        if previous_attempt:
            # 第一次点击后刷新页面已出现倒计时，说明第一次其实已经生效。
            print(f"✅ 页面已显示续期倒计时，确认续期已生效: {countdown_text}")
            return {
                "status": "success",
                "extra": f"续期已生效，{extra}",
            }
        return {"status": "not_due", "extra": extra}

    if not outer_renew_selector:
        return {"status": "failed", "error": "未找到续期按钮，页面状态未知"}

    print("🔄 点击外部续期按钮，等待验证窗口...")
    try:
        sb.sleep(2)
        sb.click(outer_renew_selector)
        sb.sleep(15)  # 等待模态框加载，可能因网络因素加载慢
    except Exception as e:
        print(f"❌ 点击外部按钮失败: {e}")
        return {"status": "failed", "error": f"点击外部续期按钮出错: {e}"}

    # 处理弹窗中的 Turnstile
    print("🔒 检测弹窗中的 Turnstile 验证...")
    turnstile_passed = False
    for captcha_attempt in range(1, 4):
        try:
            sb.uc_gui_click_captcha()
            time.sleep(12)
        except Exception as e:
            print(f"⚠️ 点击 Turnstile 出错: {e}")

        if wait_for_turnstile_pass(sb, timeout=20):
            turnstile_passed = True
            break
        print(f"⏳ 第 {captcha_attempt} 次未通过，重试点击...")

    if not turnstile_passed:
        print("❌ Turnstile 验证最终未通过")
        return {"status": "failed", "error": "Turnstile 验证未通过"}

    # 点击弹窗中的续期按钮
    print("⏳ 等待续期按钮可用并点击...")
    time.sleep(5)
    try:
        sb.click('button:contains("Renew for 4 days")', timeout=8)
        print("✅ 已点击续期按钮")
    except Exception as e:
        print(f"❌ 续期按钮点击失败: {e}")
        return {"status": "failed", "error": f"弹窗续期按钮点击失败: {e}"}

    print("⏳ 等待续期结果...")
    sb.sleep(6)

    # 续期成功后会出现新的倒计时；不再读取或比较到期日期。
    try:
        new_page_text = sb.get_page_source()
        new_match = re.search(r"Renew in (\d{2}:\d{2}:\d{2})", new_page_text)
    except Exception as e:
        print(f"❌ 读取续期结果失败: {e}")
        return {"status": "failed", "error": f"读取续期结果失败: {e}"}

    if new_match:
        new_countdown = new_match.group(1)
        print(f"✅ 续期成功！新的倒计时: {new_countdown}")
        return {
            "status": "success",
            "extra": f"⏱️ 可续期时间: {format_countdown(new_countdown)}后",
            "countdown": new_countdown,
        }

    print("⚠️ 续期结果未知，页面未出现新的倒计时")
    return {"status": "failed", "error": "续期结果未知，页面未出现新的倒计时"}


def renew_with_retry(sb):
    """续期失败时重新加载账单页，并且只额外重跑一次。"""
    renewal_result = attempt_renewal(sb, attempt_number=1)
    if renewal_result["status"] != "failed":
        return renewal_result

    first_error = renewal_result["error"]
    print(f"⚠️ 第 1 次续期失败: {first_error}")
    print("🔁 将重新打开账单页并重跑一次续期")

    if not open_billings_page(sb):
        return {
            "status": "failed",
            "error": f"{first_error}；重试前重新打开账单页失败",
        }

    return attempt_renewal(sb, attempt_number=2, previous_attempt=True)


# 主流程
def main():
    print("#" * 25)
    print("   Bot-hosting 自动续期")
    print("#" * 25)

    IS_PROXY = os.environ.get("IS_PROXY", "false").lower() == "true"
    PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
    HEADLESS = os.environ.get("HEADLESS", "false").lower() == "true" 

    sb_kwargs = {"uc": True, "headless": HEADLESS}

    if IS_PROXY:
        print(f"🔗 挂载代理: {PROXY_SERVER}")
        sb_kwargs["proxy"] = PROXY_SERVER
    else:
        print("🍭 未使用代理，直连访问")

    global _LOGIN_METHOD

    with SB(**sb_kwargs) as sb:
        try:
            ip = get_current_ip(PROXY_SERVER if IS_PROXY else "")
            print(f"📍 当前出口IP: {ip}")
        except Exception as e:
            print(f"⚠️ 获取出口 IP 失败: {e}")

        login_ok = False

        # 方式1: SESSION_TOKEN Cookie 登录（默认）
        if SESSION_TOKEN:
            print("🚀 启动浏览器...")
            open_bot_hosting_for_cookies(sb)

            print("📝 注入 Cookie...")
            for name, value in COOKIES.items():
                if value:
                    # 不强制指定 domain，让浏览器使用当前已校验的页面域名。
                    # 这样可避免代理跳转/www 子域导致 InvalidCookieDomainException。
                    sb.add_cookie({"name": name, "value": value, "path": "/"})

            print("🌐 访问 https://bot-hosting.net/a/billings ...")
            sb.open("https://bot-hosting.net/a/billings")
            sb.wait_for_ready_state_complete()
            sb.sleep(3)
            current_url = sb.get_current_url()
            current_title = sb.get_title()
            print(f"📝 当前URL: {current_url}, Title: {current_title}")

            if "/a/billings" in current_url and "/login" not in current_url and "error=" not in current_url:
                login_ok = True
                print("✅ SESSION_TOKEN 登录成功, 当前已到达账单页")
            else:
                print(f"❌ SESSION_TOKEN 登录失败，当前URL: {current_url}, 当前标题: {current_title}")

        # 方式2: Discord OAuth 登录（备用）
        if not login_ok and DC_TOKEN:
            _LOGIN_METHOD = "Discord Token"
            print("\n🔄 SESSION_TOKEN 登录失败或未配置，尝试 Discord OAuth 登录...")
            if do_discord_login(sb):
                print("🌐 访问 https://bot-hosting.net/a/billings ...")
                sb.open("https://bot-hosting.net/a/billings")
                sb.wait_for_ready_state_complete()
                sb.sleep(3)
                current_url = sb.get_current_url()
                current_title = sb.get_title()
                print(f"📝 当前URL: {current_url}, Title: {current_title}")

                if "a/billings" in current_url:
                    login_ok = True
                    print("✅ Discord OAuth 登录成功,当前已到达账单页")
                else:
                    print(f"❌ Discord OAuth 登录后仍未到达账单页，当前URL: {current_url}")
            else:
                print("❌ Discord OAuth 登录失败")

        if not login_ok:
            error_msg = "Cookie 已失效或页面异常"
            if not SESSION_TOKEN and DC_TOKEN:
                error_msg = "Discord OAuth 登录失败"
            elif SESSION_TOKEN and DC_TOKEN:
                error_msg = "SESSION_TOKEN 和 Discord OAuth 均失败"
            send_telegram_message(format_notification("❌ 登录失败", error=error_msg))
            return

        if _LOGIN_METHOD == "Discord Token":
            print("ℹ️ 本次使用 Discord OAuth 登录，新的 SESSION_TOKEN 将自动更新到 Secrets")

        # 执行续期；明确失败或结果未知时，重新打开账单页后最多再跑一次。
        renewal_result = renew_with_retry(sb)

        if renewal_result["status"] == "success":
            send_telegram_message(
                format_notification(
                    "✅ 续期成功",
                    extra=renewal_result.get("extra", "续期已生效"),
                )
            )
        elif renewal_result["status"] == "not_due":
            print(f"⏳ 未到续期时间: {renewal_result['extra']}")
            send_telegram_message(
                format_notification(
                    "⏳ 未到续期时间",
                    extra=renewal_result["extra"],
                )
            )
        else:
            error_msg = renewal_result.get("error", "未知错误")
            print(f"❌ 重跑后续期仍失败: {error_msg}")
            send_telegram_message(
                format_notification(
                    "❌ 续期失败",
                    extra="已自动重跑 1 次，请稍后手动检查",
                    error=error_msg,
                )
            )

        # 更新SESSION_TOKEN 
        print("🔄 检查 SESSION_TOKEN 是否需要更新")
        new_token, token_expiry = get_cookie_info(sb, "session_token")
        old_token = SESSION_TOKEN

        if should_update_cookie(new_token, old_token, token_expiry):
            print("🔄 SESSION_TOKEN 需要更新")
            if GH_TOKEN:
                if update_github_secret("SESSION_TOKEN", new_token):
                    print("✅ SESSION_TOKEN 更新成功")
                else:
                    print("⚠️ 更新失败，请检查 GH_TOKEN 权限")
            else:
                print("⚠️ 未设置 GH_TOKEN，无法自动更新")
                print(f"📋 请手动设置 SESSION_TOKEN = {new_token[:4]}...{new_token[-4:]}")
        else:
            print("✅ SESSION_TOKEN 无需更新")
        
        print("🏁 脚本执行完毕")

if __name__ == "__main__":
    main()
