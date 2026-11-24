import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests
from urllib3.exceptions import ReadTimeoutError


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("proxy_fallback_app", ROOT / "app.py")
app = importlib.util.module_from_spec(SPEC)
with patch.dict(os.environ, {"EMAIL": "test@example.invalid", "BOT_HOSTING_PASSWORD": "test-password"}), patch.dict(
    sys.modules, {"seleniumbase": SimpleNamespace(SB=None)}
):
    SPEC.loader.exec_module(app)


PROXY = "socks5://192.0.2.1:1080"
VPNGATE = "socks5://127.0.0.1:10080"


class RequiredProxyTests(unittest.TestCase):
    def setUp(self):
        self.account = {"id": "account_3", "name": "Test account", "email": "test@example.invalid", "password": "test-password"}
        self.kwargs = {"uc": True, "headless": False}
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.probe = stack.enter_context(patch.object(app, "proxy_is_available", return_value=True))
        self.session = stack.enter_context(patch.object(app, "renew_account_once", return_value={"status": "success"}))
        self.notify = stack.enter_context(patch.object(app, "send_saved_failure_notification"))

    def test_unavailable_external_proxy_skips_browser(self):
        self.probe.return_value = False
        result = app.renew_account(self.account, self.kwargs, PROXY)
        self.assertEqual(result["status"], "failed")
        self.session.assert_not_called()
        self.notify.assert_called_once_with(self.account, result)

    def test_external_proxy_is_used_for_the_only_browser_session(self):
        for status in ("success", "failed"):
            with self.subTest(status=status):
                self.session.reset_mock()
                self.session.return_value = {"status": status, "network_error": "ERR_PROXY_CONNECTION_FAILED"}
                self.assertEqual(app.renew_account(self.account, self.kwargs, PROXY)["status"], status)
                self.session.assert_called_once_with(self.account, self.kwargs, PROXY)

    def test_startup_failure_reports_every_selected_account_without_opening_a_browser(self):
        account_list = [self.account, dict(self.account, id="account_1")]
        with patch.dict(os.environ, {"VPNGATE_STARTUP_FAILED": "true"}), patch.object(
            app, "load_accounts", return_value=account_list
        ), patch.object(app, "select_accounts", side_effect=lambda items: items), patch.object(
            app, "SCHEDULER_URL", "https://scheduler.example.invalid"
        ), patch.object(app, "SCHEDULER_AUTH_KEY", "test-key"), patch.object(
            app, "renew_account"
        ) as renew, patch.object(app, "report_account_schedule") as report, self.assertRaises(SystemExit) as raised:
            app.main()
        self.assertEqual(raised.exception.code, 1)
        renew.assert_not_called()
        self.assertEqual([call.args[0] for call in report.call_args_list], account_list)
        self.assertEqual([call.args[1]["status"] for call in report.call_args_list], ["failed", "failed"])


class VPNGateRecoveryTests(unittest.TestCase):
    first = ("connected", "node-a", "192.0.2.1", "first-connection")
    second = ("connected", "node-b", "192.0.2.2", "second-connection")

    def setUp(self):
        self.account = {"id": "account_3", "name": "Test account", "email": "test@example.invalid", "password": "test-password"}
        self.kwargs = {"uc": True}
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.wait = stack.enter_context(patch.object(app, "wait_for_connection", side_effect=[self.first, self.second]))
        self.snapshot = stack.enter_context(patch.object(app, "vpngate_connection_snapshot", return_value=self.first))
        self.recover = stack.enter_context(patch.object(app, "request_recovery"))
        self.session = stack.enter_context(patch.object(app, "renew_account_once"))
        self.notify = stack.enter_context(patch.object(app, "send_saved_failure_notification"))

    def run_vpngate(self):
        return app.renew_account(self.account, self.kwargs, VPNGATE)

    def test_node_switch_retries_with_a_verified_proxy_connection(self):
        self.snapshot.return_value = self.second
        self.session.side_effect = [{"status": "failed", "failure_step": "login"}, {"status": "success"}]
        self.assertEqual(self.run_vpngate(), {"status": "success"})
        self.recover.assert_called_once_with(self.first)
        self.assertEqual(self.wait.call_args.kwargs["previous"], self.first)
        self.assertEqual([call.kwargs["connection"] for call in self.session.call_args_list], [self.first, self.second])
        for call in self.session.call_args_list:
            self.assertEqual(call.args[1:], (self.kwargs, VPNGATE))
        self.notify.assert_not_called()

    def test_recovery_timeout_keeps_the_failure_and_stops_after_one_browser(self):
        self.wait.side_effect = [self.first, None]
        self.session.return_value = {"status": "failed", "failure_step": "login", "network_error": "WEBDRIVER_READ_TIMEOUT"}
        result = self.run_vpngate()
        self.assertEqual(result["status"], "failed")
        self.assertIn("未恢复", result["failure_extra"])
        self.session.assert_called_once()
        self.notify.assert_called_once_with(self.account, result)

    def test_unavailable_proxy_before_login_skips_the_browser(self):
        self.wait.side_effect = [None]
        result = self.run_vpngate()
        self.assertEqual(result["status"], "failed")
        self.session.assert_not_called()
        self.recover.assert_not_called()
        self.notify.assert_called_once_with(self.account, result)

    def test_non_network_login_failure_does_not_switch_nodes(self):
        self.session.return_value = {"status": "failed", "failure_step": "login", "error": "login rejected"}
        result = self.run_vpngate()
        self.recover.assert_not_called()
        self.wait.assert_called_once()
        self.notify.assert_called_once_with(self.account, result)

    def test_network_failure_during_renewal_does_not_replay_renewal(self):
        self.session.return_value = {"status": "failed", "failure_step": "renewal", "network_error": "WEBDRIVER_READ_TIMEOUT"}
        result = self.run_vpngate()
        self.session.assert_called_once()
        self.recover.assert_not_called()
        self.notify.assert_called_once_with(self.account, result)


class ProxyProbeAndPageTests(unittest.TestCase):
    def test_runner_snapshot_records_node_and_connection_time(self):
        with patch.object(app.requests, "Session") as factory:
            session = factory.return_value.__enter__.return_value
            session.get.return_value.json.return_value = {
                "state": "connected", "current": {"hostName": "node-a", "ip": "192.0.2.1"}, "connectedAt": "2026-11-05T10:00:00Z",
            }
            self.assertEqual(app.vpngate_connection_snapshot(), ("connected", "node-a", "192.0.2.1", "2026-11-05T10:00:00Z"))
        self.assertFalse(session.trust_env)
        self.assertTrue(session.get.call_args.args[0].endswith("/status"))

    def test_probe_checks_login_and_challenge_domains_using_remote_dns(self):
        with patch.object(app.requests, "Session") as factory:
            session = factory.return_value.__enter__.return_value
            session.get.return_value.__enter__.return_value.status_code = 403
            self.assertTrue(app.proxy_is_available(VPNGATE))
        self.assertFalse(session.trust_env)
        self.assertEqual([call.args[0] for call in session.get.call_args_list], [
            "https://bot-hosting.net/login", "https://challenges.cloudflare.com/",
        ])
        for call in session.get.call_args_list:
            self.assertEqual(call.kwargs["proxies"]["https"], "socks5h://127.0.0.1:10080")

    def test_probe_transport_error_returns_false(self):
        with patch.object(app.requests, "Session") as factory, contextlib.redirect_stdout(io.StringIO()):
            factory.return_value.__enter__.return_value.get.side_effect = requests.ConnectionError("SOCKS connection failed")
            self.assertFalse(app.proxy_is_available(PROXY))

    def test_chrome_error_page_does_not_wait_for_a_missing_login_form(self):
        page = Mock()
        page.evaluate.return_value = "This site can't be reached ERR_SOCKS_CONNECTION_FAILED"
        with patch.object(app, "save_login_step_screenshot"), contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(app.do_email_password_login(page, {"id": "account_3"}))
        page.wait_for_element_visible.assert_not_called()
        page.update_text.assert_not_called()

    def test_login_navigation_read_timeout_stops_before_any_form_interaction(self):
        page = Mock()
        error = ReadTimeoutError(None, "/session/test/url", "Read timed out. (read timeout=120)")
        page.open.side_effect = error
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ReadTimeoutError):
            app.do_email_password_login(page, {"id": "account_3"})
        page.click.assert_not_called()
        page.evaluate.assert_not_called()

    def test_network_error_is_attached_to_the_final_failure(self):
        page = Mock()
        page.evaluate.return_value = "ERR_CONNECTION_RESET"
        with patch.object(app, "save_redacted_screenshot", return_value="failure.png"), patch.object(
            app, "send_saved_failure_notification"
        ):
            result = app.notify_account_failure(page, {"id": "account_1"}, "renewal", "upstream renewal failed")
        self.assertEqual(result["network_error"], "ERR_CONNECTION_RESET")


if __name__ == "__main__":
    unittest.main()
