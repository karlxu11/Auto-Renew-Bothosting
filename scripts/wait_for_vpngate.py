#!/usr/bin/env python3
"""Wait until the VPNGate runner has a usable connected SOCKS5 endpoint."""

import os
import sys
import time
import urllib.parse

import requests
from urllib3.util import Timeout


PROXY_URL = "socks5://127.0.0.1:10080"
LOGIN_URL = "https://bot-hosting.net/login"
CHALLENGE_URL = "https://challenges.cloudflare.com/"


def control_url() -> str:
    return os.environ.get("VPNGATE_CONTROL_URL", "http://127.0.0.1:18081").rstrip("/")


def is_vpngate_proxy(proxy_server: str) -> bool:
    try:
        proxy = urllib.parse.urlparse(proxy_server)
        return (
            proxy.scheme in {"socks5", "socks5h"}
            and proxy.hostname in {"127.0.0.1", "localhost"}
            and proxy.port == 10080
        )
    except ValueError:
        return False


def get_status(timeout: float = 3) -> dict:
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(f"{control_url()}/status", timeout=timeout)
        response.raise_for_status()
        return response.json()


def connection_snapshot(status: dict):
    current = status.get("current") or {}
    return status.get("state"), current.get("hostName"), current.get("ip"), status.get("connectedAt")


def request_proxies(proxy_server: str) -> dict[str, str]:
    if not proxy_server.strip():
        raise requests.exceptions.InvalidProxyURL("需要配置代理地址")
    proxy_url = proxy_server.replace("socks5://", "socks5h://", 1)
    return {"http": proxy_url, "https": proxy_url}


def probe_proxy(timeout: float, proxy_server: str = PROXY_URL) -> tuple[int, int]:
    proxies = request_proxies(proxy_server)
    deadline = time.monotonic() + timeout
    codes = []
    with requests.Session() as session:
        session.trust_env = False
        for url in (LOGIN_URL, CHALLENGE_URL):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise requests.Timeout(f"代理探测超时: {url}")
            with session.get(
                url, proxies=proxies, timeout=Timeout(total=remaining), stream=True, allow_redirects=False,
            ) as response:
                # Cloudflare 的 403/429 也能确认代理传输通路。
                codes.append(response.status_code)
    return codes[0], codes[1]


def request_recovery(connection, timeout: float = 3) -> None:
    """仅回收实际失败的隧道，Runner 已经切换时返回 409。"""
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.post(f"{control_url()}/recover", json={"connectedAt": connection[3]}, timeout=timeout)
            if response.status_code == 409:
                print("VPNGate 已开始切换连接，等待代理恢复。", flush=True)
                return
            response.raise_for_status()
            print("已请求 VPNGate 更换故障连接。", flush=True)
    except requests.RequestException as exc:
        print(f"⚠️ 请求更换 VPNGate 连接失败，等待 Runner 自动恢复: {exc}", flush=True)


def wait_for_connection(timeout: float, proxy_server: str = PROXY_URL, previous=None, poll_seconds: float = 5):
    deadline = time.monotonic() + timeout
    last_status = None
    last_error = None
    failed_probes = 0

    while (remaining := deadline - time.monotonic()) > 0:
        try:
            status = get_status(timeout=min(3, remaining))
            connection = connection_snapshot(status)
            if last_status is None or connection != last_status[0]:
                failed_probes = 0
            state, hostname, ip, connected_at = connection
            node = hostname or ip or "-"
            error = status.get("lastError") or ""

            status_key = (connection, error)
            if status_key != last_status:
                print(f"VPNGate 状态: {state or 'unknown'}，节点: {node}，错误: {error or '-'}", flush=True)
                last_status = status_key

            if state == "connected" and connected_at and (hostname or ip) and connection != previous:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    login_code, challenge_code = probe_proxy(timeout=min(10, remaining), proxy_server=proxy_server)
                except requests.RequestException:
                    failed_probes += 1
                    remaining = deadline - time.monotonic()
                    if failed_probes == 2 and remaining > 0:
                        print(f"⚠️ VPNGate 节点 {node} 连续两次未通过代理探测。", flush=True)
                        request_recovery(connection, timeout=min(3, remaining))
                    raise
                failed_probes = 0
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                confirmed = connection_snapshot(get_status(timeout=min(3, remaining)))
                if connection == confirmed and time.monotonic() < deadline:
                    print(f"VPNGate SOCKS5 已就绪：{node}，登录页 HTTP {login_code}，验证站 HTTP {challenge_code}", flush=True)
                    return connection
        except (OSError, ValueError, AttributeError) as exc:
            message = str(exc)
            if message != last_error:
                print(f"等待 VPNGate 控制接口或 SOCKS5 连通：{message}", flush=True)
                last_error = message

        time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))

    return None


def main() -> int:
    wait_seconds = int(os.environ.get("VPNGATE_WAIT_SECONDS", "300"))
    poll_seconds = int(os.environ.get("VPNGATE_POLL_SECONDS", "5"))
    print(f"等待 VPNGate Runner 连接，最长 {wait_seconds} 秒；控制接口：{control_url()}", flush=True)
    if wait_for_connection(wait_seconds, poll_seconds=poll_seconds):
        return 0

    print("VPNGate 在本轮等待时间内未建立可用代理连接。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
