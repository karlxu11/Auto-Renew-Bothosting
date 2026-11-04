import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch

import requests


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "wait_for_vpngate.py"
SPEC = importlib.util.spec_from_file_location("wait_for_vpngate", SCRIPT)
waiter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(waiter)


class FakeClock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class WaitForVPNGateTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.output = io.StringIO()
        for manager in (
            patch.object(waiter, "time", self.clock),
            patch.object(waiter, "WAIT_SECONDS", 3),
            patch.object(waiter, "POLL_SECONDS", 1),
            contextlib.redirect_stdout(self.output),
            contextlib.redirect_stderr(self.output),
        ):
            manager.__enter__()
            self.addCleanup(manager.__exit__, None, None, None)

    def test_auth_failure_times_out_without_enabling_proxy(self):
        status = {"state": "disconnected", "lastError": "AUTH_FAILED"}
        with patch.object(waiter, "get_json", return_value=status), patch.object(waiter, "probe_proxy") as probe:
            self.assertEqual(waiter.main(), 1)
        probe.assert_not_called()
        self.assertIn("回退直连继续续期", self.output.getvalue())
        self.assertEqual(self.output.getvalue().count("AUTH_FAILED"), 1)

    def test_unreachable_control_api_times_out(self):
        with patch.object(waiter, "get_json", side_effect=OSError("connection refused")):
            self.assertEqual(waiter.main(), 1)
        self.assertEqual(self.output.getvalue().count("connection refused"), 1)

    def test_connected_state_requires_working_socks_proxy(self):
        status = {"state": "connected", "current": {"hostName": "node"}}
        with patch.object(waiter, "get_json", return_value=status), patch.object(
            waiter, "probe_proxy", side_effect=requests.ConnectionError("SOCKS refused")
        ) as probe:
            self.assertEqual(waiter.main(), 1)
        self.assertEqual(probe.call_count, 3)
        self.assertNotIn("SOCKS5 已就绪", self.output.getvalue())

    def test_retries_proxy_until_it_becomes_usable(self):
        status = {"state": "connected", "current": {"hostName": "node"}}
        with patch.object(waiter, "get_json", return_value=status), patch.object(
            waiter, "probe_proxy", side_effect=[requests.Timeout("slow proxy"), 200]
        ) as probe:
            self.assertEqual(waiter.main(), 0)
        self.assertEqual(probe.call_count, 2)
        self.assertIn("SOCKS5 已就绪", self.output.getvalue())

    def test_node_changes_are_logged_even_when_state_is_unchanged(self):
        statuses = iter(["node-a", "node-b", "node-b"])

        def get_json(path, **kwargs):
            if path == "/health":
                return {}
            return {"state": "connecting", "current": {"hostName": next(statuses)}}

        with patch.object(waiter, "get_json", side_effect=get_json):
            self.assertEqual(waiter.main(), 1)
        self.assertIn("node-a", self.output.getvalue())
        self.assertEqual(self.output.getvalue().count("node-b"), 1)

    def test_cloudflare_http_response_proves_socks_connectivity(self):
        with patch.object(waiter.requests, "Session") as session_factory:
            session = session_factory.return_value.__enter__.return_value
            session.get.return_value.__enter__.return_value.status_code = 403
            self.assertEqual(waiter.probe_proxy(timeout=2), 403)
        self.assertFalse(session.trust_env)
        self.assertEqual(session.get.call_args.kwargs["proxies"]["https"], "socks5h://127.0.0.1:10080")


if __name__ == "__main__":
    unittest.main()
