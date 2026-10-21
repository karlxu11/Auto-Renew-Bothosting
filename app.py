#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sys, time, json, requests, html
import urllib.parse
from datetime import datetime, timedelta, timezone
from seleniumbase import SB

# 通过 GitHub Actions Secrets 等环境变量配置账号信息，不要写入源码。
EMAIL         = os.environ.get("EMAIL") or ""           # Bot-hosting 登录邮箱，也用于通知
BOT_HOSTING_PASSWORD = os.environ.get("BOT_HOSTING_PASSWORD") or ""  # Bot-hosting 登录密码
BOT_HOSTING_ACCOUNTS = os.environ.get("BOT_HOSTING_ACCOUNTS") or ""  # 多账号 JSON 配置
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID") or ""      # TG chat id,不填写不通知，需和bot token一起填写生效
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN") or ""    # TG bot token 
SCHEDULER_URL = os.environ.get("SCHEDULER_URL", "").strip().rstrip("/")
SCHEDULER_AUTH_KEY = os.environ.get("SCHEDULER_AUTH_KEY") or ""
SELECTED_ACCOUNT_IDS = os.environ.get("SELECTED_ACCOUNT_IDS", "").strip()

if not BOT_HOSTING_ACCOUNTS and not (EMAIL and BOT_HOSTING_PASSWORD):
    print("ℹ️ 请配置 BOT_HOSTING_ACCOUNTS，或配置 EMAIL 与 BOT_HOSTING_PASSWORD。脚本终止。")
    sys.exit(1)

# 记录本次登录方式（用于通知）
_LOGIN_METHOD = "Email + Password"
_ACCOUNT_NAME = ""


def load_accounts():
    """从多账号 JSON Secret 或旧版单账号环境变量加载账号。"""
    if BOT_HOSTING_ACCOUNTS:
        try:
            raw_accounts = json.loads(BOT_HOSTING_ACCOUNTS)
        except json.JSONDecodeError as e:
            raise ValueError("BOT_HOSTING_ACCOUNTS 必须是有效的 JSON 数组") from e

        if not isinstance(raw_accounts, list) or not raw_accounts:
            raise ValueError("BOT_HOSTING_ACCOUNTS 必须是非空 JSON 数组")

        accounts = []
        for index, raw_account in enumerate(raw_accounts, start=1):
            if not isinstance(raw_account, dict):
                raise ValueError(f"BOT_HOSTING_ACCOUNTS 第 {index} 项必须是对象")

            account_id = str(raw_account.get("id") or f"account_{index}").strip()
            email = str(raw_account.get("email") or "").strip()
            password = str(raw_account.get("password") or "")
            name = str(raw_account.get("name") or f"账号 {index}").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account_id):
                raise ValueError(f"BOT_HOSTING_ACCOUNTS 第 {index} 项的 id 格式无效")
            if not email:
                raise ValueError(f"BOT_HOSTING_ACCOUNTS 第 {index} 项缺少 email")
            if not password:
                raise ValueError(f"BOT_HOSTING_ACCOUNTS 第 {index} 项缺少 password")

            accounts.append({
                "id": account_id,
                "name": name or f"账号 {index}",
                "email": email,
                "password": password,
            })
        ids = [account["id"] for account in accounts]
        if len(ids) != len(set(ids)):
            raise ValueError("BOT_HOSTING_ACCOUNTS 中的 id 必须唯一")
        return accounts

    if not EMAIL or not BOT_HOSTING_PASSWORD:
        raise ValueError("单账号登录需要同时配置 EMAIL 和 BOT_HOSTING_PASSWORD")

    return [{
        "id": "account_1",
        "name": "账号 1",
        "email": EMAIL,
        "password": BOT_HOSTING_PASSWORD,
    }]


def select_accounts(accounts):
    """按 Worker 事件中的账号 ID 筛选；人工运行未指定时处理全部账号。"""
    raw_ids = SELECTED_ACCOUNT_IDS
    if not raw_ids or raw_ids == "null":
        return accounts

    try:
        selected_ids = json.loads(raw_ids)
    except json.JSONDecodeError as e:
        raise ValueError("SELECTED_ACCOUNT_IDS 必须是 JSON 数组") from e
    if selected_ids is None:
        return accounts
    if not isinstance(selected_ids, list) or any(not isinstance(item, str) for item in selected_ids):
        raise ValueError("SELECTED_ACCOUNT_IDS 必须是字符串数组")
    if not selected_ids:
        return []

    account_by_id = {account["id"]: account for account in accounts}
    unknown_ids = sorted(set(selected_ids) - account_by_id.keys())
    if unknown_ids:
        raise ValueError(f"触发事件包含未知账号 ID: {', '.join(unknown_ids)}")
    return [account_by_id[account_id] for account_id in dict.fromkeys(selected_ids)]


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
                    print(f"✅ Telegram 通知和续期截图已发送: {screenshot_path}")
                    return

                print(f"⚠️ Telegram 截图发送失败（HTTP {response.status_code}），改发文字通知")
            except Exception as e:
                print(f"⚠️ Telegram 截图发送异常，改发文字通知: {e}")
        elif screenshot_path:
            print(f"⚠️ 续期截图不存在，改发文字通知: {screenshot_path}")

        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        response = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": message}, timeout=10)
        response.raise_for_status()
        print("✅ Telegram 文字通知已发送")
    except Exception as e:
        print(f"❌ Telegram 发送失败: {e}")


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
def wait_for_turnstile_pass(sb, timeout=30):
    start = time.time()
    cf_indicators = ["verify you are human", "确认您是真人", "troubleshoot", "just a moment"]
    while time.time() - start < timeout:
        page_lower = sb.get_page_source().lower()
        if not any(x in page_lower for x in cf_indicators):
            print("ℹ️ 页面上已没有检测到 Cloudflare 验证提示")
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

def do_email_password_login(sb) -> bool:
    """通过 Bot-hosting 邮箱和密码登录；如需人工验证则等待验证通过。"""
    if not EMAIL or not BOT_HOSTING_PASSWORD:
        print("⚠️ 未配置 EMAIL 或 BOT_HOSTING_PASSWORD，跳过邮箱登录")
        return False

    print("\n🔑 通过 Bot-hosting 邮箱和密码登录...")
    try:
        sb.uc_open_with_reconnect("https://bot-hosting.net/login", reconnect_time=4)
        sb.wait_for_ready_state_complete()
        sb.sleep(2)

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
            sb.click(continue_selector, timeout=10)
            sb.wait_for_element_visible(password_selector, timeout=15)

        email_selector = visible_email_selector()
        if email_selector and sb.is_element_visible(email_selector):
            sb.update_text(email_selector, EMAIL)
        sb.update_text(password_selector, BOT_HOSTING_PASSWORD)

        # 不尝试绕过 Turnstile；常规通过时继续，遇到挑战则超时并报告登录失败。
        if not wait_for_turnstile_pass(sb, timeout=30):
            sb.save_screenshot("login_turnstile_timeout.png")
            return False

        sb.click(sign_in_selector, timeout=10)
        for _ in range(60):
            current_url = sb.get_current_url()
            parsed_url = urllib.parse.urlparse(current_url)
            if (
                parsed_url.hostname in {"bot-hosting.net", "www.bot-hosting.net"}
                and parsed_url.path != "/login"
                and not parsed_url.path.startswith("/login/")
            ):
                print("✅ 邮箱和密码登录成功")
                return True
            # 有些前端会先更新会话状态，再由后续导航改变 URL。
            # 账单页访问会再次确认会话是否有效。
            if not sb.is_element_visible(password_selector):
                print("ℹ️ 登录表单已关闭，将通过打开账单页确认登录状态")
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
        sb.save_screenshot("login_email_timeout.png")
        return False
    except Exception as e:
        print(f"❌ 邮箱和密码登录失败: {e}")
        try:
            sb.save_screenshot("login_email_error.png")
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

    print("⏳ 等待续期结果 10 秒...")
    time.sleep(10)
    renewal_screenshot = save_renewal_screenshot(sb, attempt_number)

    # 续期成功后会出现新的倒计时；不再读取或比较到期日期。
    try:
        new_page_text = sb.get_page_source()
        new_countdown = extract_renewal_countdown(new_page_text)
    except Exception as e:
        print(f"❌ 读取续期结果失败: {e}")
        return {
            "status": "failed",
            "error": f"读取续期结果失败: {e}",
            "screenshot": renewal_screenshot,
        }

    if new_countdown:
        print(f"✅ 续期成功！新的倒计时: {new_countdown}")
        return {
            "status": "success",
            "extra": f"⏱️ 可续期时间: {format_countdown(new_countdown)}后",
            "countdown": new_countdown,
            "screenshot": renewal_screenshot,
        }

    print("⚠️ 续期结果未知，页面未出现新的倒计时")
    return {
        "status": "failed",
        "error": "续期结果未知，页面未出现新的倒计时",
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


# 为一个账号完成登录和续期；每个账号使用独立浏览器会话。
def renew_account(account, sb_kwargs, proxy_server, is_proxy):
    global EMAIL, BOT_HOSTING_PASSWORD
    global _LOGIN_METHOD, _ACCOUNT_NAME

    EMAIL = account["email"]
    BOT_HOSTING_PASSWORD = account["password"]
    _ACCOUNT_NAME = account["name"]
    _LOGIN_METHOD = "Email + Password"

    with SB(**sb_kwargs) as sb:
        try:
            ip = get_current_ip(proxy_server if is_proxy else "")
            print(f"📍 当前出口IP: {ip}")
        except Exception as e:
            print(f"⚠️ 获取出口 IP 失败: {e}")

        login_ok = False

        if EMAIL and BOT_HOSTING_PASSWORD and do_email_password_login(sb):
            if open_billings_page(sb):
                login_ok = True
                print("✅ 邮箱和密码登录成功，当前已到达账单页")
            else:
                print("❌ 邮箱登录后未能打开账单页")

        if not login_ok:
            send_telegram_message(format_notification("❌ 登录失败", error="邮箱密码登录失败"))
            return {"status": "failed", "error": "邮箱密码登录失败"}

        print("ℹ️ 本次使用邮箱和密码登录")

        # 执行续期；明确失败或结果未知时，重新打开账单页后最多再跑一次。
        renewal_result = renew_with_retry(sb)

        if renewal_result["status"] == "success":
            success_extra = renewal_result.get("extra", "续期已生效")
            send_telegram_message(
                format_notification("✅ 续期成功", extra=success_extra),
                screenshot_path=renewal_result.get("screenshot", ""),
            )
        elif renewal_result["status"] == "not_due":
            print(f"⏳ 未到续期时间: {renewal_result['extra']}")
            send_telegram_message(
                format_notification("⏳ 未到续期时间", extra=renewal_result["extra"]),
                screenshot_path=renewal_result.get("screenshot", ""),
            )
        else:
            error_msg = renewal_result.get("error", "未知错误")
            print(f"❌ 重跑后续期仍失败: {error_msg}")
            send_telegram_message(
                format_notification(
                    "❌ 续期失败",
                    extra="已自动重跑 1 次，请稍后手动检查",
                    error=error_msg,
                ),
                screenshot_path=renewal_result.get("screenshot", ""),
            )

    print(f"✅ 账号 {account['name']} 的续期流程完成")
    return renewal_result


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
        print(f"\n========== 开始续期账号 {index}/{len(accounts)} ==========")
        result = {"status": "failed"}
        try:
            result = renew_account(account, sb_kwargs, proxy_server, is_proxy) or result
        except Exception as e:
            failed_accounts += 1
            EMAIL = account["email"]
            BOT_HOSTING_PASSWORD = account["password"]
            _ACCOUNT_NAME = account["name"]
            _LOGIN_METHOD = "Email + Password"
            print(f"❌ 账号 {index} 执行异常: {e}")
            send_telegram_message(format_notification("❌ 账号续期异常", error=str(e)))
        report_account_schedule(account, result)

    print(f"🏁 多账号续期处理完成：共处理 {len(accounts)} 个账号，未捕获异常 {failed_accounts} 个")

if __name__ == "__main__":
    main()
