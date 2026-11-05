"""Load renewal accounts and resolve the account selection before starting a VPN."""

import json
import os
from pathlib import Path
import re
import sys


ACCOUNT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
ACCOUNT_DISPLAY_NAMES = {
    "account_1": "karlxu28",
    "account_2": "karlxu0",
    "account_3": "karlzhe",
}


def format_account_label(account_id):
    name = ACCOUNT_DISPLAY_NAMES.get(account_id)
    return f"{account_id}（{name}）" if name else account_id


def load_accounts():
    """从多账号 JSON Secret 或单账号环境变量加载账号。"""
    raw_config = os.environ.get("BOT_HOSTING_ACCOUNTS", "")
    email = os.environ.get("EMAIL", "")
    password = os.environ.get("BOT_HOSTING_PASSWORD", "")
    if not raw_config:
        if not email or not password:
            raise ValueError("单账号登录需要同时配置 EMAIL 和 BOT_HOSTING_PASSWORD")
        return [{"id": "account_1", "name": "账号 1", "email": email, "password": password}]

    try:
        raw_accounts = json.loads(raw_config)
    except json.JSONDecodeError as exc:
        raise ValueError("BOT_HOSTING_ACCOUNTS 必须是有效的 JSON 数组") from exc
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
        if not ACCOUNT_ID_PATTERN.fullmatch(account_id):
            raise ValueError(f"BOT_HOSTING_ACCOUNTS 第 {index} 项的 id 格式无效")
        if not email:
            raise ValueError(f"BOT_HOSTING_ACCOUNTS 第 {index} 项缺少 email")
        if not password:
            raise ValueError(f"BOT_HOSTING_ACCOUNTS 第 {index} 项缺少 password")
        accounts.append({"id": account_id, "name": name or f"账号 {index}", "email": email, "password": password})

    ids = [account["id"] for account in accounts]
    if len(ids) != len(set(ids)):
        raise ValueError("BOT_HOSTING_ACCOUNTS 中的 id 必须唯一")
    return accounts


def select_accounts(accounts, raw_ids=None):
    """接受 Worker 的 JSON 数组或手动输入的逗号/空白分隔 ID；留空处理全部。"""
    if raw_ids is None:
        raw_ids = os.environ.get("SELECTED_ACCOUNT_IDS", "")
    raw_ids = raw_ids.strip()
    if not raw_ids or raw_ids == "null":
        return accounts

    if raw_ids.startswith(("[", "{", '"')):
        try:
            selected_ids = json.loads(raw_ids)
        except json.JSONDecodeError as exc:
            raise ValueError("账号选择必须是有效的 JSON 数组，或逗号分隔的账号 ID") from exc
    else:
        selected_ids = re.split(r"[,\s]+", raw_ids)
    if not isinstance(selected_ids, list) or any(not isinstance(item, str) for item in selected_ids):
        raise ValueError("SELECTED_ACCOUNT_IDS 必须是字符串数组，或逗号分隔的账号 ID")
    selected_ids = [item.strip() for item in selected_ids]
    if any(not ACCOUNT_ID_PATTERN.fullmatch(item) for item in selected_ids):
        raise ValueError("账号 ID 只能包含字母、数字、下划线或连字符，长度为 1–64")

    account_by_id = {account["id"]: account for account in accounts}
    unknown_ids = sorted(set(selected_ids) - account_by_id.keys())
    if unknown_ids:
        raise ValueError(f"触发事件包含未知账号 ID: {', '.join(unknown_ids)}")
    return [account_by_id[account_id] for account_id in dict.fromkeys(selected_ids)]


def main():
    try:
        accounts = select_accounts(load_accounts())
    except ValueError as exc:
        print(f"❌ 账号配置或选择错误: {exc}", file=sys.stderr)
        return 1

    ids = [account["id"] for account in accounts]
    label = ", ".join(format_account_label(account_id) for account_id in ids) or "无账号"
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    print(f"🧾 本次续期账号（{len(ids)} 个）：{label}", flush=True)
    print(f"🔁 第 {attempt} 次运行；Re-run jobs 沿用原来的账号选择。", flush=True)

    # 只导出账号 ID；登录凭据继续由 Secrets 单独提供给续期步骤。
    env_path = os.environ.get("GITHUB_ENV")
    if env_path:
        with Path(env_path).open("a", encoding="utf-8") as output:
            output.write(f"SELECTED_ACCOUNT_IDS={json.dumps(ids)}\n")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write("### 本次续期账号\n\n")
            summary.write(f"账号数量：{len(ids)}；运行次数：{attempt}\n\n")
            summary.write("\n".join(
                f"- `{account_id}`" + (f" → {ACCOUNT_DISPLAY_NAMES[account_id]}" if account_id in ACCOUNT_DISPLAY_NAMES else "")
                for account_id in ids
            ) or "无账号，无需续期。")
            summary.write("\n\nRe-run jobs 沿用原来的账号选择。更换账号请使用 Run workflow，并填写 account_ids。\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
