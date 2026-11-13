#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sys, time, json, requests, random, subprocess, ctypes
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
BASE_URL = "https://bot-hosting.net"
NETWORK_ERROR_CODES = (
    "ERR_SOCKS_CONNECTION_FAILED", "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED", "ERR_CONNECTION_TIMED_OUT",
    "ERR_NAME_NOT_RESOLVED", "ERR_NETWORK_CHANGED", "ERR_INTERNET_DISCONNECTED",
    "ERR_CONNECTION_REFUSED", "ERR_TIMED_OUT",
)


def browser_network_error(sb) -> str:
    """只读取可见错误代码，识别 Chrome 网络错误页。"""
    try:
        text = sb.evaluate("(document.body && document.body.innerText) || ''")
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
def format_notification(status: str, extra: str = "", error: str = "", expiry_date: str = "") -> str:
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
    if expiry_date:
        lines.append(f"📅 到期时间: {expiry_date}")
    if _LOGIN_METHOD:
        lines.append(f"🔐 登录方式: {_LOGIN_METHOD}")
    if extra:
        lines.append(extra)
    if error:
        lines.append(f"⚠️ 错误信息: {error}")
    lines.append(f"⏱️ 登录时间: {now}")
    return "\n".join(lines)

# ---------- 基础工具 ----------
def js(sb, script, default=None, errors=None):
    try:
        result = sb.execute_script(script)
        return default if result is None else result
    except Exception as e:
        if errors is not None:
            errors.append(f"execute_script: {type(e).__name__}: {e}")
        try:
            result = sb.cdp.evaluate("(function(){ " + script + " })()")
            return default if result is None else result
        except Exception as e2:
            if errors is not None:
                errors.append(f"cdp.evaluate: {type(e2).__name__}: {e2}")
            return default

# Turnstile 定位失败时的现场诊断快照
def diag_turnstile_dom(sb, errors=None):
    snap = js(sb, """
        var out = [];
        var ifr = document.querySelectorAll('iframe');
        out.push('top-doc iframes: ' + ifr.length);
        for (var i = 0; i < ifr.length; i++) {
            var r = ifr[i].getBoundingClientRect();
            out.push('iframe w=' + Math.round(r.width) + ' h=' + Math.round(r.height) + ' src=' + (ifr[i].src || '').slice(0, 90));
        }
        var ts = document.querySelectorAll('[class*="turnstile"], [id*="turnstile"], input[name="cf-turnstile-response"]');
        out.push('turnstile-ish elements: ' + ts.length);
        for (var j = 0; j < ts.length; j++) {
            var rr = ts[j].getBoundingClientRect();
            var cls = (typeof ts[j].className === 'string') ? ts[j].className : (ts[j].getAttribute('class') || '');
            out.push(' - ' + ts[j].tagName + ' class=' + cls + ' w=' + Math.round(rr.width) + ' h=' + Math.round(rr.height));
        }
        out.push('window.turnstile=' + (typeof window.turnstile) + ' readyState=' + document.readyState);
        return out.join('\\n');
    """, "")
    for line in str(snap).splitlines():
        print("   [diag] " + line)
    if errors:
        for err in errors:
            print("   [diag][exec-error] " + str(err)[:200])

#  页面注入脚本
# Turnstile token（官方 JS 接口 或 表单隐藏域），未通过时为空串
TURNSTILE_TOKEN_JS = """
try { if (window.turnstile && window.turnstile.getResponse) {
        var t = window.turnstile.getResponse(); if (t) return t; } } catch (e) {}
var el = document.querySelector('input[name="cf-turnstile-response"]');
return el ? (el.value || '') : '';
"""

TURNSTILE_EXISTS_JS = """
return document.querySelector(
    'input[name="cf-turnstile-response"], iframe[src*="challenges.cloudflare.com"], .cf-turnstile'
) !== null;
"""
TS_COORDS_JS = """
function findWidget(root) {
    var els = root.querySelectorAll(
        'iframe[src*="challenges.cloudflare.com"], .cf-turnstile, [id*="cf-chl"]');
    for (var i = 0; i < els.length; i++) {
        var r = els[i].getBoundingClientRect();
        if (r.width > 50 && r.height > 20) return els[i];
    }
    var all = root.querySelectorAll('*');
    for (var j = 0; j < all.length; j++) {
        if (all[j].shadowRoot) {
            var f = findWidget(all[j].shadowRoot);
            if (f) return f;
        }
    }
    return null;
}
function boxFromTokenInput() {
    var el = document.querySelector('input[name="cf-turnstile-response"]');
    var cur = el ? el.parentElement : null;
    for (var k = 0; k < 6 && cur; k++) {
        var r = cur.getBoundingClientRect();
        if (r.width > 100 && r.width < 600 && r.height > 30 && r.height < 140) return cur;
        cur = cur.parentElement;
    }
    return null;
}
var hit = findWidget(document) || boxFromTokenInput();
if (!hit) return null;
var hr = hit.getBoundingClientRect();
return {cx: Math.round(hr.x + 30), cy: Math.round(hr.y + hr.height / 2)};
"""

# Turnstile 脚本异步渲染，轮询等待组件出现
def wait_turnstile_present(sb, timeout=12):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if js(sb, TURNSTILE_EXISTS_JS, False):
            return True
        time.sleep(1)
    return False

def _activate_window():
    if os.name == "nt":
        return

    for cls in ["chrome", "chromium", "Chromium", "Chrome", "google-chrome"]:
        try:
            r = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", cls], capture_output=True, text=True, timeout=3)
            wids = [w for w in r.stdout.strip().split("\n") if w.strip()]
            if wids:
                subprocess.run(["xdotool", "windowactivate", "--sync", wids[0]], timeout=3, stderr=subprocess.DEVNULL)
                time.sleep(0.2)
                return
        except Exception:
            pass
    try:
        subprocess.run(["xdotool", "getactivewindow", "windowactivate"], timeout=3, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def _windows_mouse_click(x: int, y: int):
    try:
        user32 = ctypes.windll.user32
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.08)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.03)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
    except Exception:
        pass

def _xdotool_click(x: int, y: int):
    if os.name == "nt":
        _windows_mouse_click(x, y)
        return

    _activate_window()
    try:
        subprocess.run(["xdotool", "mousemove", "--sync", str(x), str(y)], timeout=3, stderr=subprocess.DEVNULL)
        time.sleep(0.15)
        subprocess.run(["xdotool", "click", "1"], timeout=2, stderr=subprocess.DEVNULL)
    except Exception:
        os.system(f"xdotool mousemove {x} {y} click 1 2>/dev/null")

def turnstile_token(sb) -> str:
    token = js(sb, TURNSTILE_TOKEN_JS, "")
    return token if isinstance(token, str) else ""

def _click_turnstile(sb, locate_timeout=10) -> bool:
    coords = None
    errors = []
    deadline = time.time() + locate_timeout
    while time.time() < deadline:
        coords = js(sb, TS_COORDS_JS, None, errors)
        if coords:
            break
        time.sleep(1)
    if not coords:
        print("⚠️ 无法定位 Turnstile 坐标, 打印现场诊断:")
        diag_turnstile_dom(sb, errors)
        return False

    try:
        wi = js(sb, "return '' + (window.screenX || 0) + '|' + (window.screenY || 0) + '|' +"
                    " window.outerHeight + '|' + window.innerHeight;", "")
        parts = str(wi).split("|")
        sx, sy = int(parts[0] or 0), int(parts[1] or 0)
        bar = int(parts[2] or 800) - int(parts[3] or 768)
        ax = int(coords["cx"]) + sx
        ay = int(coords["cy"]) + sy + bar
        print(f"🖱️ 底层点击 Turnstile ({ax}, {ay})")
        _xdotool_click(ax, ay)
        time.sleep(3)
        return True
    except Exception as e:
        print(f"⚠️ 底层点击失败: {e}")
        return False

def click_turnstile(sb) -> bool:
    if _click_turnstile(sb):
        return True
    coords = js(sb, TS_COORDS_JS)
    if coords:
        try:
            if hasattr(sb, "cdp") and hasattr(sb.cdp, "gui_click_x_y"):
                print(f"🖱️ 坐标点击 Turnstile ({coords['cx']},{coords['cy']})")
                sb.cdp.gui_click_x_y(int(coords["cx"]), int(coords["cy"]))
                return True
        except Exception as e:
            print(f"⚠️ 坐标点击失败: {e}")
    try:
        if hasattr(sb, "cdp") and hasattr(sb.cdp, "click_captcha"):
            print("🖱️ 兜底调用cdp点击 ...")
            sb.cdp.click_captcha()
            return True
        print("⚠️ 当前 SeleniumBase 版本无 sb.cdp.click_captcha")
    except Exception as e:
        print(f"⚠️ click_captcha 出错: {e}")
    return False

def solve_turnstile(sb, timeout=90, tag="Turnstile", first_wait=8) -> bool:
    if turnstile_token(sb):
        return True

    deadline = time.time() + timeout
    next_click = time.time() + first_wait
    clicks = 0
    while time.time() < deadline:
        if turnstile_token(sb):
            print(f"✅ {tag}已通过(已获取 token)")
            return True
        if time.time() >= next_click:
            clicks += 1
            print(f"🖱️ 第 {clicks} 次点击 Turnstile 验证框...")
            click_turnstile(sb)
            next_click = time.time() + 10 + random.uniform(0, 3)
        time.sleep(2)
    ok = bool(turnstile_token(sb))
    print(f"{'✅' if ok else '❌'} {tag}{'已通过' if ok else '未通过'}")
    return ok

def handle_turnstile(sb) -> bool:
    print("🛡️ 处理 Turnstile 验证...")
    return solve_turnstile(sb, timeout=90, tag="Turnstile", first_wait=8)

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

# 归一化日期为 YYYY/MM/DD
def normalize_date(date_str: str) -> str:
    parts = date_str.split('/')
    if len(parts) == 3:
        if len(parts[2]) == 4:   # MM/DD/YYYY
            return f"{parts[2]}/{int(parts[0]):02d}/{int(parts[1]):02d}"
        if len(parts[0]) == 4:   # YYYY/MM/DD
            return f"{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}"
    return date_str

DATE_RE = r"(\d{4}/\d{1,2}/\d{1,2}|\d{1,2}/\d{1,2}/\d{4})"

# 获取过期日期
DATE_RE = r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})"
def extract_expiry_date(page_source: str) -> str:
    patterns = [
        r"[Ee]xpires\s*[:\-]?\s*" + DATE_RE,   # 优先匹配包含 "Expires" 的
        DATE_RE,                                # 兜底匹配
    ]

    for pattern in patterns:
        match = re.search(pattern, page_source)
        if match:
            # match.group(1) 现在能准确拿到完整的日期字符串
            return normalize_date(match.group(1))

    return None

# 优先从 DOM 的 st-meta 元素提取到期日期 (不依赖页面语言)
def get_expiry_from_dom(sb) -> str:
    for selector in ['span.st-meta', '.st-meta', '[class*="st-meta"]']:
        try:
            if sb.is_element_present(selector):
                match = re.search(DATE_RE, sb.get_text(selector))
                if match:
                    return normalize_date(match.group(1))
        except Exception:
            pass
    return None

# 登录和续期流程移植自 eooce/Auto-Renew-Bothosting@3835a43。
# 返回结构化结果，供本项目的多账号、截图通知和 Worker 调度使用。
def do_email_password_login(sb, account) -> bool:
    """按上游最新版打开邮箱表单、填写凭据并提交登录。"""
    try:
        print(f"🌐 访问 {BASE_URL}/login")
        sb.open(f"{BASE_URL}/login")
        sb.wait_for_ready_state_complete()
        sb.sleep(3)
        # 保留 VPNGate 的网络错误识别，让外层能够重建直连会话。
        if browser_network_error(sb):
            return False
        sb.click('button[type="submit"]')
        sb.sleep(12)
        print("📧 填写邮箱...")
        sb.input('input[type="email"]', account["email"])
        sb.sleep(1)
        print("🔑 填写密码...")
        sb.input('input[type="password"]', account["password"])
        sb.sleep(3)

        if wait_turnstile_present(sb):
            if not handle_turnstile(sb):
                print("❌ 登录界面的 Turnstile 验证失败，尝试兜底点击...")
                try:
                    sb.cdp.click_captcha()
                    sb.sleep(4)
                except Exception as exc:
                    print(f"⚠️ 兜底点击失败: {exc}")
        else:
            print("ℹ️ 未检测到 Turnstile")
        sb.sleep(1)
        sb.click('button[type="submit"]')
        print("🖱️ 提交登录按钮")
        sb.sleep(6)
        sb.wait_for_ready_state_complete()
        # 与上游一样，由后续账单页导航确认实际登录结果。
        return True
    except Exception as exc:
        print(f"❌ Password 登录过程异常: {type(exc).__name__}: {str(exc)[:150]}")
        return False


def open_billings_page(sb) -> bool:
    """按上游最新版导航到账单页并确认登录状态。"""
    try:
        sb.open(f"{BASE_URL}/dash/billings")
        sb.wait_for_ready_state_complete()
        sb.sleep(3)
        current_url = sb.get_current_url()
        print(f"📝 当前URL: {current_url}, Title: {sb.get_title()}")
        return "/dash/billings" in current_url and "/login" not in current_url and "error=" not in current_url
    except Exception as exc:
        print(f"❌ 打开账单页失败: {exc}")
        return False


def renew_current_page(sb):
    """执行上游最新版的单次续期；仅将通知转换为调度所需的结果。"""
    sb.sleep(3)
    try:
        page_source = sb.get_page_text() + "\n" + sb.get_page_source()
    except Exception:
        page_source = sb.get_page_source()
    current_expiry = get_expiry_from_dom(sb) or extract_expiry_date(page_source)
    if current_expiry:
        print(f"📅 当前到期日期: {current_expiry}")
    else:
        print("⚠️ 未能提取当前到期日期")

    outer_renew_selector = None
    countdown_text = None
    possible_selectors = [
        'type:button("Renew free plan")',
        'button:contains("Renew free plan")',
        '[class*="btn btn-primary btn-sm"]',
    ]
    for selector in possible_selectors:
        try:
            if sb.is_element_visible(selector):
                button_text = sb.get_text(selector)
                match = re.search(r"(?:Renew in|Renewal cooldown)\s+(\d{1,2}:\d{2}:\d{2})", button_text, re.I)
                if match:
                    countdown_text = match.group(1)
                    print(f"⏳ 续期冷却中, 未到续期时间: '{button_text}'")
                    break
                elif "Renew" in button_text:
                    outer_renew_selector = selector
                    print(f"✅ 续期按钮可用: '{button_text}'")
                    break
        except Exception:
            pass

    if not outer_renew_selector:
        if countdown_text:
            return {
                "status": "not_due", "countdown": countdown_text,
                "extra": f"⏱️ 可续期时间: {format_countdown(countdown_text)}后",
                "expiry_date": current_expiry or "（未获取到）",
            }
        # 上游的“状态未知”映射为失败，供 Worker 安排后续检查。
        return {"status": "failed", "error": "未找到续期按钮或倒计时，当前状态未知，请手动检查"}

    print("🔄 点击外部续期按钮，等待验证窗口...")
    try:
        sb.sleep(2)
        sb.click(outer_renew_selector)
        sb.sleep(15)
    except Exception as exc:
        print(f"❌ 点击外部按钮失败: {exc}")
        return {"status": "failed", "error": "点击外部续期按钮出错"}

    turnstile_passed = True
    if wait_turnstile_present(sb):
        turnstile_passed = handle_turnstile(sb)
        if not turnstile_passed:
            print("❌ Turnstile 验证失败，尝试兜底点击...")
            try:
                sb.cdp.click_captcha()
                sb.sleep(4)
                turnstile_passed = bool(turnstile_token(sb))
            except Exception as exc:
                print(f"⚠️ 兜底点击失败: {exc}")
    else:
        print("ℹ️ 未检测到 Turnstile")
    if not turnstile_passed:
        return {"status": "failed", "error": "Turnstile 验证未通过"}

    print("⏳ 等待续期按钮可用并点击...")
    time.sleep(5)
    try:
        sb.click('div[role="dialog"] button.btn-primary', timeout=8)
        print("✅ 已点击续期按钮")
    except Exception as exc:
        print(f"续期按钮点击失败: {exc}")

    print("⏳ 等待新的过期时间...")
    sb.sleep(6)
    try:
        new_page_text = sb.get_page_text() + "\n" + sb.get_page_source()
    except Exception:
        new_page_text = sb.get_page_source()
    new_expiry = get_expiry_from_dom(sb) or extract_expiry_date(new_page_text)
    new_match = re.search(r"(?:Renewal cooldown|Renew in)\s+(\d{1,2}:\d{2}:\d{2})", new_page_text)
    if new_match:
        new_countdown = new_match.group(1)
        print(f"✅ 续期成功！新的倒计时: {new_countdown}")
        result = {
            "status": "success", "countdown": new_countdown,
            "extra": f"⏱️ 可续期时间: {format_countdown(new_countdown)}后",
            "expiry_date": new_expiry or "（未获取到）",
        }
    elif new_expiry and new_expiry != current_expiry:
        print(f"✅ 续期成功，到期日期已更新为: {new_expiry}")
        result = {"status": "success", "extra": "到期日期已更新", "expiry_date": new_expiry}
    else:
        return {"status": "failed", "error": "续期结果未知，到期日期未变化，请手动检查"}

    result["screenshot"] = save_renewal_screenshot(sb, attempt_number=1)
    return result


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
            renewal_result = renew_current_page(sb)
            if renewal_result["status"] not in {"success", "not_due"}:
                error_msg = renewal_result.get("error", "未知错误")
                print(f"❌ 续期未完成: {error_msg}")
                return notify_account_failure(
                    sb, account, step, error_msg,
                    extra="截图为本次最后失败页面。",
                    notify=not defer_failures,
                )

            if renewal_result["status"] == "success":
                send_telegram_message(
                    format_notification("✅ 续期成功", extra=renewal_result.get("extra", "续期已生效"), expiry_date=renewal_result.get("expiry_date", "")),
                    screenshot_path=renewal_result.get("screenshot", ""),
                )
            elif renewal_result["status"] == "not_due":
                print(f"⏳ 未到续期时间: {renewal_result['extra']}")
                send_telegram_message(
                    format_notification("⏳ 未到续期时间", extra=renewal_result["extra"], expiry_date=renewal_result.get("expiry_date", "")),
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
    sb_kwargs = {"uc": True, "headless": headless, "locale_code": "en-US"}

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
