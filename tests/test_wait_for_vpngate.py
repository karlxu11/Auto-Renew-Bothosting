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
    connected = {"state": "connected", "current": {"hostName": "node"}, "connectedAt": "first-connection"}

    def setUp(self):
        self.clock = FakeClock()
        self.output = io.StringIO()
        for manager in (
            patch.object(waiter, "time", self.clock),
            patch.dict(waiter.os.environ, {
                "VPNGATE_WAIT_SECONDS": "3", "VPNGATE_POLL_SECONDS": "1", "VPNGATE_CONTROL_URL": "http://127.0.0.1:18081",
            }),
            contextlib.redirect_stdout(self.output),
            contextlib.redirect_stderr(self.output),
        ):
            manager.__enter__()
            self.addCleanup(manager.__exit__, None, None, None)

    def test_auth_failure_times_out_without_enabling_proxy(self):
        status = {"state": "disconnected", "lastError": "AUTH_FAILED"}
        with patch.object(waiter, "get_status", return_value=status), patch.object(waiter, "probe_proxy") as probe:
            self.assertEqual(waiter.main(), 1)
        probe.assert_not_called()
        self.assertIn("未建立可用代理连接", self.output.getvalue())
        self.assertEqual(self.output.getvalue().count("AUTH_FAILED"), 1)

    def test_unreachable_control_api_times_out(self):
        with patch.object(waiter, "get_status", side_effect=OSError("connection refused")):
            self.assertEqual(waiter.main(), 1)
        self.assertEqual(self.output.getvalue().count("connection refused"), 1)

    def test_connected_state_requires_working_socks_proxy(self):
        with patch.object(waiter, "get_status", return_value=self.connected), patch.object(
            waiter, "probe_proxy", side_effect=requests.ConnectionError("SOCKS refused")
        ) as probe, patch.object(waiter, "request_recovery") as recover:
            self.assertEqual(waiter.main(), 1)
        self.assertEqual(probe.call_count, 3)
        recover.assert_called_once()
        self.assertNotIn("SOCKS5 已就绪", self.output.getvalue())

    def test_retries_proxy_until_it_becomes_usable(self):
        with patch.object(waiter, "get_status", return_value=self.connected), patch.object(
            waiter, "probe_proxy", side_effect=[requests.Timeout("slow proxy"), (200, 403)]
        ) as probe:
            self.assertEqual(waiter.main(), 0)
        self.assertEqual(probe.call_count, 2)
        self.assertIn("SOCKS5 已就绪", self.output.getvalue())

    def test_node_changes_are_logged_even_when_state_is_unchanged(self):
        statuses = iter(["node-a", "node-b", "node-b"])

        def get_status(**kwargs):
            return {"state": "connecting", "current": {"hostName": next(statuses)}}

        with patch.object(waiter, "get_status", side_effect=get_status):
            self.assertEqual(waiter.main(), 1)
        self.assertIn("node-a", self.output.getvalue())
        self.assertEqual(self.output.getvalue().count("node-b"), 1)

    def test_cloudflare_http_response_proves_socks_connectivity(self):
        with patch.object(waiter.requests, "Session") as session_factory:
            session = session_factory.return_value.__enter__.return_value
            session.get.return_value.__enter__.return_value.status_code = 403
            self.assertEqual(waiter.probe_proxy(timeout=2), (403, 403))
        self.assertFalse(session.trust_env)
        self.assertEqual(session.get.call_args.kwargs["proxies"]["https"], "socks5h://127.0.0.1:10080")
        self.assertEqual([call.args[0] for call in session.get.call_args_list], [waiter.LOGIN_URL, waiter.CHALLENGE_URL])
        self.assertTrue(all(not call.kwargs["allow_redirects"] for call in session.get.call_args_list))

    def test_connection_change_during_probe_requires_a_fresh_probe(self):
        replacement = dict(self.connected, connectedAt="second-connection")
        with patch.object(waiter, "get_status", side_effect=[self.connected, replacement, replacement, replacement]), patch.object(
            waiter, "probe_proxy", return_value=(200, 403)
        ) as probe:
            self.assertEqual(waiter.main(), 0)
        self.assertEqual(probe.call_count, 2)

    def test_two_failed_probes_request_a_new_node_and_verify_it(self):
        replacement = dict(self.connected, current={"hostName": "node-b"}, connectedAt="second-connection")
        with patch.object(waiter, "get_status", side_effect=[self.connected, self.connected, replacement, replacement]), patch.object(
            waiter, "probe_proxy", side_effect=[requests.Timeout("login"), requests.Timeout("challenge"), (200, 403)]
        ) as probe, patch.object(waiter, "request_recovery") as recover:
            self.assertEqual(waiter.wait_for_connection(3, poll_seconds=1), waiter.connection_snapshot(replacement))
        self.assertEqual(probe.call_count, 3)
        recover.assert_called_once()
        self.assertEqual(recover.call_args.args[0], waiter.connection_snapshot(self.connected))

    def test_probe_with_an_empty_proxy_makes_no_http_request(self):
        with patch.object(waiter.requests, "Session") as session, self.assertRaises(requests.exceptions.InvalidProxyURL):
            waiter.probe_proxy(timeout=2, proxy_server=" ")
        session.assert_not_called()

    def test_recovery_waits_for_a_different_connection_even_on_the_same_node(self):
        replacement = dict(self.connected, connectedAt="second-connection")
        previous = waiter.connection_snapshot(self.connected)
        with patch.object(waiter, "get_status", side_effect=[self.connected, replacement, replacement]), patch.object(
            waiter, "probe_proxy", return_value=(200, 403)
        ) as probe:
            self.assertEqual(waiter.wait_for_connection(3, previous=previous, poll_seconds=1), waiter.connection_snapshot(replacement))
        probe.assert_called_once()

    def test_recovery_request_targets_failed_connection_and_accepts_stale_response(self):
        with patch.object(waiter.requests, "Session") as session_factory:
            session = session_factory.return_value.__enter__.return_value
            response = session.post.return_value
            response.status_code = 409
            waiter.request_recovery(waiter.connection_snapshot(self.connected))
        session.post.assert_called_once_with(
            "http://127.0.0.1:18081/recover", json={"connectedAt": "first-connection"}, timeout=3,
        )
        response.raise_for_status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
