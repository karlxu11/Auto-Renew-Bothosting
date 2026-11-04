#!/usr/bin/env python3
"""Wait until the VPNGate runner has a usable connected SOCKS5 endpoint."""

import json
import os
import sys
import time
import urllib.error
import urllib.request

import requests


CONTROL_URL = os.environ.get("VPNGATE_CONTROL_URL", "http://127.0.0.1:18081")
WAIT_SECONDS = int(os.environ.get("VPNGATE_WAIT_SECONDS", "300"))
POLL_SECONDS = int(os.environ.get("VPNGATE_POLL_SECONDS", "5"))
PROXY_URL = "socks5h://127.0.0.1:10080"
PROBE_URL = "https://bot-hosting.net/"


def get_json(path: str, timeout: float = 8) -> dict:
    request = urllib.request.Request(f"{CONTROL_URL}{path}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def probe_proxy(timeout: float) -> int:
    # 实际经过 SOCKS5、远端 DNS 和 TLS，避免只凭控制接口状态挂载不可用代理。
    with requests.Session() as session:
        session.trust_env = False
        with session.get(
            PROBE_URL,
            proxies={"http": PROXY_URL, "https": PROXY_URL},
            timeout=timeout,
            stream=True,
        ) as response:
            # Cloudflare 的 403/429 也说明隧道已连通，不能据此判定 VPN 认证失败。
            return response.status_code


def main() -> int:
    deadline = time.monotonic() + WAIT_SECONDS
    last_status = None
    last_error = None

    print(f"等待 VPNGate Runner 连接，最长 {WAIT_SECONDS} 秒；控制接口：{CONTROL_URL}")

    while time.monotonic() < deadline:
        try:
            get_json("/health", timeout=min(8, max(0.1, deadline - time.monotonic())))
            status = get_json("/status", timeout=min(8, max(0.1, deadline - time.monotonic())))
            state = status.get("state", "unknown")
            current = status.get("current") or {}
            node = current.get("hostName") or current.get("ip") or "-"
            error = status.get("lastError") or ""

            status_key = (state, node, error)
            if status_key != last_status:
                print(f"VPNGate 状态: {state}，节点: {node}，错误: {error or '-'}", flush=True)
                last_status = status_key

            if state == "connected":
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                status_code = probe_proxy(timeout=min(10, remaining))
                print(f"VPNGate SOCKS5 已就绪：{node} -> socks5://127.0.0.1:10080，目标站 HTTP {status_code}", flush=True)
                return 0
        except (OSError, urllib.error.URLError, json.JSONDecodeError, requests.RequestException) as exc:
            message = str(exc)
            if message != last_error:
                print(f"等待 VPNGate 控制接口或 SOCKS5 连通：{message}", flush=True)
                last_error = message

        time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))

    print("VPNGate 在限定时间内没有建立可用连接；工作流将回退直连继续续期。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
