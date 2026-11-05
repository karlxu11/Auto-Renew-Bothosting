#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sys, time, json, requests, html
import urllib.parse
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from seleniumbase import SB
from accounts import format_account_label, load_accounts, select_accounts

# 通过 GitHub Actions Secrets 等环境变量配置账号信息，不要写入源码。
EMAIL         = os.environ.get("EMAIL") or ""           # Bot-hosting 登录邮箱，也用于通知
BOT_HOSTING_PASSWORD = os.environ.get("BOT_HOSTING_PASSWORD") or ""  # Bot-hosting 登录密码
BOT_HOSTING_ACCOUNTS = os.environ.get("BOT_HOSTING_ACCOUNTS") or ""  # 多账号 JSON 配置
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID") or ""      # TG chat id,不填写不通知，需和bot token一起填写生效
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN") or ""    # TG bot token 
SCHEDULER_URL = os.environ.get("SCHEDULER_URL", "").strip().rstrip("/")
SCHEDULER_AUTH_KEY = os.environ.get("SCHEDULER_AUTH_KEY") or ""

if not BOT_HOSTING_ACCOUNTS and not (EMAIL and BOT_HOSTING_PASSWORD):
    print("ℹ️ 请配置 BOT_HOSTING_ACCOUNTS，或配置 EMAIL 与 BOT_HOSTING_PASSWORD。脚本终止。")
    sys.exit(1)

# 记录本次登录方式（用于通知）
_LOGIN_METHOD = "Email + Password"
_ACCOUNT_NAME = ""
NETWORK_ERROR_CODES = (
    "ERR_SOCKS_CONNECTION_FAILED", "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED", "ERR_CONNECTION_TIMED_OUT",
    "ERR_NAME_NOT_RESOLVED", "ERR_NETWORK_CHANGED", "ERR_INTERNET_DISCONNECTED",
    "ERR_CONNECTION_REFUSED", "ERR_TIMED_OUT",
)


def browser_network_error(sb) -> str:
    """只读取可见错误代码，识别 Chrome 网络错误页。"""
    try:
        text = sb.execute_script("return (document.body && document.body.innerText) || '';")
        if isinstance(text, str):
            return next((code for code in NETWORK_ERROR_CODES if code in text), "")
    except Exception:
        pass
    return ""


def proxy_is_available(proxy_server: str) -> bool:
    """复核登录站点和验证域名的代理通路；HTTP 403 也表示网络已连通。"""
    proxy_url = proxy_server.replace("socks5://", "socks5h://", 1)
    try:
        with requests.Session() as session:
            session.trust_env = False
            for url in ("https://bot-hosting.net/login", "https://challenges.cloudflare.com/"):
                with session.get(url, proxies={"http": proxy_url, "https": proxy_url}, timeout=8, stream=True, allow_redirects=False):
                    pass
        return True
    except requests.RequestException as exc:
        print(f"⚠️ 代理连通性复核失败: {exc}", flush=True)
        return False


def vpngate_connection_snapshot(proxy_server: str):
    """记录本地 Runner 的节点和建连时间，识别验证期间曾断线又恢复的会话。"""
    proxy = urllib.parse.urlparse(proxy_server)
    if proxy.hostname not in {"127.0.0.1", "localhost"} or proxy.port != 10080:
        return None
    control_url = os.environ.get("VPNGATE_CONTROL_URL", "http://127.0.0.1:18081").rstrip("/")
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(f"{control_url}/status", timeout=3)
            response.raise_for_status()
            status = response.json()
        current = status.get("current") or {}
        return (status.get("state"), current.get("hostName"), current.get("ip"), status.get("connectedAt"))
    except (requests.RequestException, ValueError, AttributeError):
        return None


def countdown_to_seconds(countdown: str):
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2})", str(countdown or "").strip())
    if not match:
        return None
    hours, minutes, seconds = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds


def report_account_schedule(account, result):
    """将单个账号的下次检查时间回写给 Cloudflare Worker。"""
    status = (result or {}).get("status", "failed")
    countdown = (result or {}).get("countdown")
    seconds = countdown_to_seconds(countdown) if countdown else None

    now = datetime.now(timezone.utc)
    if status in {"success", "not_due"} and seconds is not None:
        # 留两分钟余量，避免倒计时边界和页面更新延迟导致过早触发。
        next_run = now + timedelta(seconds=max(60, seconds) + 120)
    elif status == "success":
        # 如果续期成功但页面没给出倒计时，明天重新检查并恢复准确时间。
        next_run = now + timedelta(hours=24)
    else:
        # 登录、代理或续期异常时延后重试，避免每小时重复启动同一账号。
        next_run = now + timedelta(hours=6)

    payload = {
        "account_id": account["id"],
        "status": status,
        "next_run_at": next_run.isoformat().replace("+00:00", "Z"),
    }
    try:
        response = requests.post(
            f"{SCHEDULER_URL}/api/schedule",
            headers={
                "Authorization": f"Bearer {SCHEDULER_AUTH_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        if not response.ok:
            detail = re.sub(r"\s+", " ", response.text or "").strip()[:240]
            raise requests.HTTPError(
                f"HTTP {response.status_code}" + (f": {detail}" if detail else ""),
                response=response,
            )
        print(f"🗓️ 已登记账号 {account['name']} 的下次检查时间: {payload['next_run_at']}")
    except Exception as e:
        print(f"❌ 回写账号 {account['name']} 的调度时间失败: {e}")
        if getattr(e, "response", None) is not None and e.response.status_code == 401:
            print("🔧 请确认 Cloudflare Worker 与 GitHub Actions 中的 SCHEDULER_AUTH_KEY 完全相同，且 Worker Secret 已部署。")

# 发送tg通知
def send_telegram_message(message: str, screenshot_path: str = ""):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，跳过通知")
        return

    try:
        if screenshot_path and os.path.isfile(screenshot_path):
            try:
                url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
                caption = message[:1021] + "..." if len(message) > 1024 else message
                with open(screenshot_path, "rb") as screenshot:
                    response = requests.post(
                        url,
                        data={"chat_id": TG_CHAT_ID, "caption": caption},
                        files={
                            "photo": (
                                os.path.basename(screenshot_path),
                                screenshot,
                                "image/png",
                            )
                        },
                        timeout=30,
                    )

                if response.ok:
                    print(f"✅ Telegram 通知和截图已发送: {screenshot_path}")
                    return

                print(f"⚠️ Telegram 截图发送失败（HTTP {response.status_code}），改发文字通知")
            except Exception as e:
                print(f"⚠️ Telegram 截图发送异常，改发文字通知: {e}")
        elif screenshot_path:
            print(f"⚠️ 截图不存在，改发文字通知: {screenshot_path}")

        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        response = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": message}, timeout=10)
        response.raise_for_status()
        print("✅ Telegram 文字通知已发送")
    except Exception as e:
        print(f"❌ Telegram 发送失败: {e}")


def save_redacted_screenshot(sb, screenshot_path: str) -> str:
    """隐藏凭据输入框后截图；脱敏失败时跳过截图，最后恢复页面样式。"""
    try:
        sb.execute_script(
            """
            for (const el of document.querySelectorAll(
              'input[type="email"], input[name="email"], input[autocomplete="email"], input[type="password"], input[type="text"]'
            )) {
              el.setAttribute('data-login-screenshot-style', el.getAttribute('style') || '');
              el.style.setProperty('color', 'transparent', 'important');
              el.style.setProperty('-webkit-text-fill-color', 'transparent', 'important');
              el.style.setProperty('text-shadow', 'none', 'important');
              el.style.setProperty('caret-color', 'transparent', 'important');
            }
            """
        )
        sb.save_screenshot(screenshot_path)
        if os.path.isfile(screenshot_path):
            print(f"📸 脱敏截图已保存: {screenshot_path}")
            return screenshot_path
        print(f"⚠️ 截图未生成: {screenshot_path}")
    except Exception as e:
        print(f"⚠️ 保存脱敏截图失败: {e}")
    finally:
        try:
            sb.execute_script(
                """
                for (const el of document.querySelectorAll('[data-login-screenshot-style]')) {
                  const oldStyle = el.getAttribute('data-login-screenshot-style');
                  if (oldStyle) el.setAttribute('style', oldStyle);
                  else el.removeAttribute('style');
                  el.removeAttribute('data-login-screenshot-style');
                }
                """
            )
        except Exception:
            pass

    return ""


def save_login_step_screenshot(sb, account, step: str) -> str:
    """仅在本地保存登录中间步骤的脱敏截图。"""
    account_id = re.sub(r"[^A-Za-z0-9_-]", "_", account.get("id", "account"))
    step_id = re.sub(r"[^A-Za-z0-9_-]", "_", step)
    return save_redacted_screenshot(sb, f"login_{account_id}_{step_id}.png")


def notify_account_failure(sb, account, step: str, error: str, extra: str = "", notify: bool = True) -> dict:
    """在浏览器关闭前留存现场；代理恢复期间将通知延后到最终结果。"""
    network_error = browser_network_error(sb) or next((code for code in NETWORK_ERROR_CODES if code in error), "")
    if network_error:
        error = f"页面网络错误 {network_error}；{error}"
    account_id = re.sub(r"[^A-Za-z0-9_-]", "_", account.get("id", "account"))
    step_id = re.sub(r"[^A-Za-z0-9_-]", "_", step)
    # 使用唯一文件名，防止截图失败时误用之前的步骤或其他账号的旧图。
    screenshot_path = save_redacted_screenshot(sb, f"failure_{account_id}_{step_id}_{uuid4().hex}.png")
    result = {
        "status": "failed", "error": error, "screenshot": screenshot_path, "failure_step": step,
        "failure_extra": extra, "network_error": network_error,
    }
    if notify:
        send_saved_failure_notification(account, result)
    return result


def send_saved_failure_notification(account, result):
    step = result.get("failure_step", "账号执行")
    step_label = {"login": "登录", "billings": "打开账单页", "renewal": "续期"}.get(step, step)
    screenshot_path = result.get("screenshot", "")
    detail = f"👤 账号映射: {format_account_label(account['id'])}\n失败步骤: {step_label}"
    if result.get("failure_extra"):
        detail += f"\n{result['failure_extra']}"
    if not screenshot_path:
        detail += "\n当前页面无法截图，已发送文字说明。"
    send_telegram_message(
        format_notification(f"❌ {step_label}失败", extra=detail, error=result.get("error", "未知错误")),
        screenshot_path=screenshot_path,
    )


def save_renewal_screenshot(sb, attempt_number: int) -> str:
    """保存续期点击后的页面截图，并返回截图路径。"""
    screenshot_path = f"renew_result_{attempt_number}.png"
    try:
        sb.save_screenshot(screenshot_path)
        if os.path.isfile(screenshot_path):
            print(f"📸 续期结果截图已保存: {screenshot_path}")
            return screenshot_path
        print(f"⚠️ 续期截图未生成: {screenshot_path}")
    except Exception as e:
        print(f"⚠️ 保存续期截图失败: {e}")
    return ""

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
        f"👤 登录账户: {_ACCOUNT_NAME} ({masked_email})" if _ACCOUNT_NAME else f"👤 登录账户: {masked_email}",
    ]
    if _LOGIN_METHOD:
        lines.append(f"🔐 登录方式: {_LOGIN_METHOD}")
    if extra:
        lines.append(extra)
    if error:
        lines.append(f"⚠️ 错误信息: {error}")
    lines.append(f"⏱️ 登录时间: {now}")
    return "\n".join(lines)

# 等待Turnstile验证通过
def wait_for_turnstile_pass(sb, timeout=30, submit_selector=None):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        network_error = browser_network_error(sb)
        if network_error:
            print(f"❌ 验证页面网络连接失败: {network_error}")
            return False
        # 验证提示位于跨域 iframe 内，不能用外层 HTML 中没有提示来判断通过。
        verified = sb.execute_script(
            """
            const responses = document.querySelectorAll('[name="cf-turnstile-response"]');
            if ([...responses].some(el => (el.value || '').trim())) return true;
            try {
              return Boolean(window.turnstile && window.turnstile.getResponse());
            } catch (_) {
              return false;
            }
            """
        )
        # token 出现和前端解锁按钮可能不同步，两个条件必须同时满足。
        if verified and (
            not submit_selector
            or (
                sb.is_element_visible(submit_selector)
                and sb.is_element_enabled(submit_selector)
                and sb.get_attribute(submit_selector, "aria-disabled", hard_fail=False) != "true"
            )
        ):
            print("✅ Cloudflare 验证已通过" + ("，续期按钮已启用" if submit_selector else ""))
            return True
        sb.sleep(1)
    print("❌ Turnstile 验证超时或提交按钮仍未启用")
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

def do_email_password_login(sb, account) -> bool:
    """填写邮箱密码、点击 Cloudflare 复选框，验证通过后提交登录。"""
    if not EMAIL or not BOT_HOSTING_PASSWORD:
        print("⚠️ 未配置 EMAIL 或 BOT_HOSTING_PASSWORD，跳过邮箱登录")
        return False

    print("\n🔑 通过 Bot-hosting 邮箱和密码登录...")
    try:
        sb.uc_open_with_reconnect("https://bot-hosting.net/login", reconnect_time=4)
        sb.wait_for_ready_state_complete()
        sb.sleep(2)
        save_login_step_screenshot(sb, account, "01_login_page")
        network_error = browser_network_error(sb)
        if network_error:
            print(f"❌ 登录页面未加载: {network_error}")
            return False

        email_selectors = [
            'input[type="email"]',
            'input[name="email"]',
            'input[autocomplete="email"]',
            'input[placeholder="Email"]',
            'input[aria-label="Email"]',
            'input[type="text"]',
        ]
        password_selector = 'input[type="password"]'
        continue_selector = 'button:contains("Continue with email")'
        sign_in_selector = 'button:contains("Sign in")'

        def visible_email_selector():
            for selector in email_selectors:
                try:
                    if sb.is_element_visible(selector):
                        return selector
                except Exception:
                    continue
            return ""

        if not sb.is_element_visible(password_selector):
            email_selector = visible_email_selector()
            if not email_selector:
                sb.wait_for_element_visible(email_selectors[0], timeout=15)
                email_selector = email_selectors[0]
            sb.update_text(email_selector, EMAIL)
            save_login_step_screenshot(sb, account, "02_email_entered")
            sb.click(continue_selector, timeout=10)
            save_login_step_screenshot(sb, account, "03_continue_with_email")
            sb.wait_for_element_visible(password_selector, timeout=15)
            save_login_step_screenshot(sb, account, "04_password_page")

        email_selector = visible_email_selector()
        if email_selector and sb.is_element_visible(email_selector):
            sb.update_text(email_selector, EMAIL)
        sb.update_text(password_selector, BOT_HOSTING_PASSWORD)
        save_login_step_screenshot(sb, account, "05_credentials_entered")

        print("⏳ 邮箱和密码已填写，等待 10 秒后点击 Cloudflare 验证框...")
        sb.sleep(10)
        save_login_step_screenshot(sb, account, "06_wait_10_seconds")

        turnstile_passed = False
        for captcha_attempt in range(1, 4):
            print(f"🔒 点击登录页 Cloudflare 验证框（第 {captcha_attempt}/3 次）...")
            try:
                sb.uc_gui_click_captcha()
                save_login_step_screenshot(sb, account, f"07_cloudflare_checkbox_clicked_{captcha_attempt}")
            except Exception as e:
                print(f"⚠️ 点击登录页 Cloudflare 验证框出错: {e}")

            if wait_for_turnstile_pass(sb, timeout=30):
                turnstile_passed = True
                break
            if browser_network_error(sb):
                break

        if not turnstile_passed:
            save_login_step_screenshot(sb, account, "07_verification_wait_timeout")
            return False

        save_login_step_screenshot(sb, account, "07_verification_passed")
        sb.click(sign_in_selector, timeout=10)
        save_login_step_screenshot(sb, account, "08_sign_in_clicked")
        for _ in range(60):
            current_url = sb.get_current_url()
            parsed_url = urllib.parse.urlparse(current_url)
            if (
                parsed_url.hostname in {"bot-hosting.net", "www.bot-hosting.net"}
                and parsed_url.path != "/login"
                and not parsed_url.path.startswith("/login/")
            ):
                print("✅ 邮箱和密码登录成功")
                save_login_step_screenshot(sb, account, "09_login_success")
                return True
            # 有些前端会先更新会话状态，再由后续导航改变 URL。
            # 账单页访问会再次确认会话是否有效。
            if not sb.is_element_visible(password_selector):
                print("ℹ️ 登录表单已关闭，将通过打开账单页确认登录状态")
                save_login_step_screenshot(sb, account, "09_login_form_closed")
                return True
            time.sleep(0.5)

        current_url = sb.get_current_url()
        parsed_url = urllib.parse.urlparse(current_url)
        print(
            "❌ 邮箱登录未完成："
            f"页面路径仍为 {parsed_url.path or '/'}，"
            f"密码框仍显示={sb.is_element_visible(password_selector)}"
        )
        feedback = get_login_feedback(sb)
        if feedback:
            for message in feedback:
                print(f"🔎 登录页面提示: {message}")
        else:
            print("🔎 页面没有提供可识别的登录错误提示；请检查账号密码或登录页截图")
        save_login_step_screenshot(sb, account, "09_login_failed")
        return False
    except Exception as e:
        print(f"❌ 邮箱和密码登录失败: {e}")
        try:
            save_login_step_screenshot(sb, account, "error")
        except Exception:
            pass
        return False


def get_login_feedback(sb):
    """读取页面上的可见错误提示，不读取或记录输入框内容。"""
    try:
        messages = sb.execute_script(
            """
            const selectors = [
              '[role="alert"]', '[aria-live="assertive"]',
              '[data-sonner-toast]', '[class*="error" i]'
            ];
            const nodes = [...new Set(selectors.flatMap(s => [...document.querySelectorAll(s)]))];
            return nodes
              .filter(el => {
                const style = getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden' && el.getClientRects().length;
              })
              .map(el => (el.innerText || '').trim())
              .filter(Boolean)
              .slice(0, 6);
            """
        ) or []
        safe_messages = []
        for message in messages:
            sanitized = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email]", str(message))
            sanitized = re.sub(r"\s+", " ", sanitized).strip()[:240]
            if sanitized and sanitized not in safe_messages:
                safe_messages.append(sanitized)
        return safe_messages
    except Exception:
        return []


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
            countdown = extract_renewal_countdown(button_text)
            if countdown:
                return None, countdown

            button_text_lower = button_text.lower()
            if "renew" in button_text_lower and "cooldown" not in button_text_lower:
                print(f"✅ 续期按钮可用: '{button_text}'")
                return selector, None
        except Exception:
            # 某个候选选择器失效不应影响后续选择器的检查。
            continue

    # Bot-hosting 当前账单页显示的是 “Renewal cooldown HH:MM:SS”，
    # 其结构不一定会被上面的按钮选择器匹配到，因此再从整页文本识别一次。
    try:
        countdown = extract_renewal_countdown(sb.get_page_source())
        if countdown:
            return None, countdown
    except Exception:
        pass

    return None, None


def extract_renewal_countdown(page_text: str):
    """从可见文本或 HTML 中识别 Bot-hosting 的续期冷却倒计时。"""
    plain_text = html.unescape(re.sub(r"<[^>]*>", " ", page_text or ""))
    match = re.search(
        r"\b(?:renew\s+in|renewal\s+cooldown)\s*:?[ \t]*(\d{1,2}:\d{2}:\d{2})\b",
        plain_text,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def wait_for_renewal_countdown(sb, timeout=45):
    """轮询续期结果，允许代理和页面渲染延迟，不重复提交续期请求。"""
    deadline = time.monotonic() + timeout
    while True:
        countdown = extract_renewal_countdown(sb.get_page_source())
        if countdown:
            return countdown
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        sb.sleep(min(1, remaining))


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
                "countdown": countdown_text,
            }
        return {"status": "not_due", "extra": extra, "countdown": countdown_text}

    if not outer_renew_selector:
        return {"status": "failed", "error": "未找到续期按钮，页面状态未知"}

    print("🔄 点击外部续期按钮，等待验证窗口...")
    submit_selector = 'button:contains("Renew for 4 days")'
    try:
        sb.click(outer_renew_selector)
        sb.wait_for_element_visible(submit_selector, timeout=30)
    except Exception as e:
        print(f"❌ 点击外部按钮失败: {e}")
        return {
            "status": "failed",
            "error": f"打开续期弹窗失败: {e}",
            "screenshot": save_renewal_screenshot(sb, attempt_number),
        }

    # 处理弹窗中的 Turnstile
    print("🔒 检测弹窗中的 Turnstile 验证...")
    turnstile_passed = False
    for captcha_attempt in range(1, 4):
        print(f"🔒 点击续期弹窗 Cloudflare 验证框（第 {captcha_attempt}/3 次）...")
        try:
            sb.uc_gui_click_captcha()
        except Exception as e:
            print(f"⚠️ 点击 Turnstile 出错: {e}")

        if wait_for_turnstile_pass(sb, timeout=30, submit_selector=submit_selector):
            turnstile_passed = True
            break
        print(f"⏳ 第 {captcha_attempt} 次未通过，重试点击...")

    if not turnstile_passed:
        print("❌ Turnstile 验证最终未通过")
        return {
            "status": "failed",
            "error": "续期弹窗 Turnstile 未通过或续期按钮未启用，未提交续期",
            "screenshot": save_renewal_screenshot(sb, attempt_number),
        }

    # 点击弹窗中的续期按钮
    print("⏳ 提交续期...")
    try:
        sb.wait_for_element_clickable(submit_selector, timeout=8)
        sb.click(submit_selector, timeout=8)
        print("ℹ️ 已点击续期按钮，等待账单倒计时确认结果")
    except Exception as e:
        print(f"❌ 续期按钮点击失败: {e}")
        return {
            "status": "failed",
            "error": f"弹窗续期按钮点击失败: {e}",
            "screenshot": save_renewal_screenshot(sb, attempt_number),
        }

    # 续期成功后会出现新的倒计时；不再读取或比较到期日期。
    print("⏳ 等待续期倒计时更新（最多 45 秒）...")
    try:
        new_countdown = wait_for_renewal_countdown(sb)
    except Exception as e:
        print(f"❌ 读取续期结果失败: {e}")
        return {
            "status": "failed",
            "error": f"读取续期结果失败: {e}",
            "screenshot": save_renewal_screenshot(sb, attempt_number),
        }

    renewal_screenshot = save_renewal_screenshot(sb, attempt_number)
    if new_countdown:
        print(f"✅ 续期成功！新的倒计时: {new_countdown}")
        return {
            "status": "success",
            "extra": f"⏱️ 可续期时间: {format_countdown(new_countdown)}后",
            "countdown": new_countdown,
            "screenshot": renewal_screenshot,
        }

    for message in get_login_feedback(sb):
        print(f"🔎 续期页面提示: {message}")
    print("⚠️ 等待 45 秒后仍未出现新的续期倒计时")
    return {
        "status": "failed",
        "error": "续期提交后等待 45 秒，页面仍未出现新的倒计时",
        "screenshot": renewal_screenshot,
    }


def renew_with_retry(sb):
    """续期失败时重新加载账单页，并且只额外重跑一次。"""
    renewal_result = attempt_renewal(sb, attempt_number=1)
    if renewal_result["status"] != "failed":
        return renewal_result

    first_error = renewal_result["error"]
    print(f"⚠️ 第 1 次续期失败: {first_error}")
    print("🔁 将重新打开账单页并重跑一次续期")

    first_screenshot = renewal_result.get("screenshot", "")
    if not open_billings_page(sb):
        return {
            "status": "failed",
            "error": f"{first_error}；重试前重新打开账单页失败",
            "screenshot": first_screenshot,
        }

    retry_result = attempt_renewal(sb, attempt_number=2, previous_attempt=True)
    if not retry_result.get("screenshot") and first_screenshot:
        retry_result["screenshot"] = first_screenshot
    return retry_result


# 为一个账号执行一次浏览器会话。
def renew_account_once(account, sb_kwargs, proxy_server, is_proxy, defer_failures=False):
    global EMAIL, BOT_HOSTING_PASSWORD
    global _LOGIN_METHOD, _ACCOUNT_NAME

    EMAIL = account["email"]
    BOT_HOSTING_PASSWORD = account["password"]
    _ACCOUNT_NAME = account["name"]
    _LOGIN_METHOD = "Email + Password"

    with SB(**sb_kwargs) as sb:
        step = "login"
        try:
            try:
                ip = get_current_ip(proxy_server if is_proxy else "")
                print(f"📍 当前出口IP: {ip}")
            except Exception as e:
                print(f"⚠️ 获取出口 IP 失败: {e}")

            if not (EMAIL and BOT_HOSTING_PASSWORD and do_email_password_login(sb, account)):
                return notify_account_failure(
                    sb, account, step, "登录页验证或邮箱密码登录未完成，详情见日志和失败截图",
                    notify=not defer_failures,
                )

            step = "billings"
            if not open_billings_page(sb):
                return notify_account_failure(sb, account, step, "邮箱登录后未能打开账单页", notify=not defer_failures)
            print("✅ 邮箱和密码登录成功，当前已到达账单页")
            print("ℹ️ 本次使用邮箱和密码登录")

            step = "renewal"
            # 明确失败或结果未知时，重新打开账单页后最多再跑一次。
            renewal_result = renew_with_retry(sb)
            if renewal_result["status"] not in {"success", "not_due"}:
                error_msg = renewal_result.get("error", "未知错误")
                print(f"❌ 重跑后续期仍失败: {error_msg}")
                return notify_account_failure(
                    sb, account, step, error_msg,
                    extra="已自动重跑 1 次；截图为本次最后失败页面。",
                    notify=not defer_failures,
                )

            if renewal_result["status"] == "success":
                send_telegram_message(
                    format_notification("✅ 续期成功", extra=renewal_result.get("extra", "续期已生效")),
                    screenshot_path=renewal_result.get("screenshot", ""),
                )
            elif renewal_result["status"] == "not_due":
                print(f"⏳ 未到续期时间: {renewal_result['extra']}")
                send_telegram_message(
                    format_notification("⏳ 未到续期时间", extra=renewal_result["extra"]),
                    screenshot_path=renewal_result.get("screenshot", ""),
                )

            print(f"✅ 账号 {account['name']} 的续期流程完成")
            return renewal_result
        except Exception as e:
            print(f"❌ 账号 {format_account_label(account['id'])} 执行异常: {e}")
            return notify_account_failure(sb, account, step, f"执行异常: {e}", notify=not defer_failures)


def renew_account(account, sb_kwargs, proxy_server, is_proxy):
    """代理启动后再次检查；运行中失效时，用全新直连会话最多重跑一次。"""
    if not is_proxy:
        return renew_account_once(account, sb_kwargs, proxy_server, False)

    use_direct = not proxy_is_available(proxy_server)
    if not use_direct:
        connection_before = vpngate_connection_snapshot(proxy_server)
        try:
            result = renew_account_once(account, sb_kwargs, proxy_server, True, defer_failures=True)
        except Exception as exc:
            if not any(code in str(exc) for code in NETWORK_ERROR_CODES) and proxy_is_available(proxy_server):
                raise
            print(f"⚠️ 代理浏览器启动或连接异常: {exc}", flush=True)
            use_direct = True
        else:
            if result.get("status") != "failed":
                return result
            connection_after = vpngate_connection_snapshot(proxy_server)
            connection_changed = bool(connection_before and connection_after and connection_before != connection_after)
            if connection_changed:
                print("⚠️ 验证期间 VPNGate 节点或建连时间已变化，原浏览器会话需要重建。", flush=True)
            use_direct = connection_changed or bool(result.get("network_error")) or not proxy_is_available(proxy_server)
            if not use_direct:
                send_saved_failure_notification(account, result)
                return result

    print(f"⚠️ {format_account_label(account['id'])} 的代理不可用，将用全新浏览器直连重试一次。", flush=True)
    direct_kwargs = dict(sb_kwargs)
    direct_kwargs.pop("proxy", None)
    result = renew_account_once(account, direct_kwargs, "", False)
    result["direct_fallback"] = True
    return result


# 主流程
def main():
    global EMAIL, BOT_HOSTING_PASSWORD, _ACCOUNT_NAME, _LOGIN_METHOD

    print("#" * 25)
    print("   Bot-hosting 自动续期")
    print("#" * 25)

    try:
        accounts = load_accounts()
    except ValueError as e:
        print(f"❌ 账号配置错误: {e}")
        sys.exit(1)

    if not SCHEDULER_URL or not SCHEDULER_AUTH_KEY:
        print("❌ 自动调度需要配置 SCHEDULER_URL 和 SCHEDULER_AUTH_KEY。脚本终止。")
        sys.exit(1)

    try:
        accounts = select_accounts(accounts)
    except ValueError as e:
        print(f"❌ 账号筛选错误: {e}")
        sys.exit(1)

    if not accounts:
        print("ℹ️ 本次没有选中的账号，无需处理。")
        return

    print(f"🧾 本次选中账号: {', '.join(format_account_label(account['id']) for account in accounts)}")

    is_proxy = os.environ.get("IS_PROXY", "false").lower() == "true"
    proxy_server = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
    headless = os.environ.get("HEADLESS", "false").lower() == "true"
    sb_kwargs = {"uc": True, "headless": headless}

    if is_proxy:
        print(f"🔗 挂载代理: {proxy_server}")
        sb_kwargs["proxy"] = proxy_server
    else:
        print("🍭 未使用代理，直连访问")

    failed_accounts = 0
    for index, account in enumerate(accounts, start=1):
        print(f"\n========== 开始续期账号 {index}/{len(accounts)} · {format_account_label(account['id'])} ==========")
        result = {"status": "failed"}
        try:
            result = renew_account(account, sb_kwargs, proxy_server, is_proxy) or result
        except Exception as e:
            EMAIL = account["email"]
            BOT_HOSTING_PASSWORD = account["password"]
            _ACCOUNT_NAME = account["name"]
            _LOGIN_METHOD = "Email + Password"
            print(f"❌ 账号 {index} 执行异常: {e}")
            send_telegram_message(format_notification("❌ 账号续期异常", error=str(e)))
        if result.get("direct_fallback"):
            is_proxy = False
            proxy_server = ""
            sb_kwargs.pop("proxy", None)
            print("🌐 本次运行后续账号也将使用直连。", flush=True)
        report_account_schedule(account, result)
        if result.get("status") == "failed":
            failed_accounts += 1

    print(f"🏁 多账号续期处理完成：共处理 {len(accounts)} 个账号，失败 {failed_accounts} 个")
    if failed_accounts:
        sys.exit(1)

if __name__ == "__main__":
    main()
