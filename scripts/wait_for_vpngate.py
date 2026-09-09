#!/usr/bin/env python3
"""Wait until the VPNGate runner has a usable connected SOCKS5 endpoint."""

import json
import os
import sys
import time
import urllib.error
import urllib.request


CONTROL_URL = os.environ.get("VPNGATE_CONTROL_URL", "http://127.0.0.1:18081")
WAIT_SECONDS = int(os.environ.get("VPNGATE_WAIT_SECONDS", "300"))
POLL_SECONDS = int(os.environ.get("VPNGATE_POLL_SECONDS", "5"))


def get_json(path: str) -> dict:
    request = urllib.request.Request(f"{CONTROL_URL}{path}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.load(response)


def main() -> int:
    deadline = time.monotonic() + WAIT_SECONDS
    last_state = None
    last_error = None

    print(f"等待 VPNGate Runner 连接，最长 {WAIT_SECONDS} 秒；控制接口：{CONTROL_URL}")

    while time.monotonic() < deadline:
        try:
            get_json("/health")
            status = get_json("/status")
            state = status.get("state", "unknown")
            current = status.get("current") or {}
            node = current.get("hostName") or current.get("ip") or "-"
            error = status.get("lastError") or ""

            if state != last_state or error != last_error:
                print(f"VPNGate 状态: {state}，节点: {node}，错误: {error or '-'}", flush=True)
                last_state = state
                last_error = error

            if state == "connected":
                print(f"VPNGate SOCKS5 已就绪：{node} -> socks5://127.0.0.1:10080", flush=True)
                return 0
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            message = str(exc)
            if message != last_error:
                print(f"等待 Runner 控制接口：{message}", flush=True)
                last_error = message

        time.sleep(POLL_SECONDS)

    print("VPNGate 在限定时间内没有建立可用连接。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
