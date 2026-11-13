"""Verify upstream login and solver integration with per-account credentials."""

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("upstream_compatibility_app", ROOT / "app.py")
app = importlib.util.module_from_spec(SPEC)
with patch.dict(os.environ, {"EMAIL": "test@example.invalid", "BOT_HOSTING_PASSWORD": "test-password"}), patch.dict(
    sys.modules, {"seleniumbase": SimpleNamespace(SB=None)}
):
    SPEC.loader.exec_module(app)


class UpstreamLoginTests(unittest.TestCase):
    def setUp(self):
        for manager in (
            contextlib.redirect_stdout(io.StringIO()),
            patch.object(app, "browser_network_error", return_value=""),
            patch.object(app, "wait_turnstile_present", return_value=False),
        ):
            manager.__enter__()
            self.addCleanup(manager.__exit__, None, None, None)
        self.page = Mock()
        self.account = {"id": "account_2", "email": "second@example.invalid", "password": "second-password"}

    def test_latest_login_opens_form_then_uses_the_selected_account_credentials(self):
        self.assertTrue(app.do_email_password_login(self.page, self.account))
        self.page.open.assert_called_once_with("https://bot-hosting.net/login")
        self.assertEqual(self.page.input.call_args_list, [
            call('input[type="email"]', "second@example.invalid"),
            call('input[type="password"]', "second-password"),
        ])
        self.assertEqual(self.page.click.call_args_list, [call('button[type="submit"]'), call('button[type="submit"]')])
        self.assertEqual(self.page.sleep.call_args_list, [call(3), call(12), call(1), call(3), call(1), call(6)])
        self.assertLess(self.page.method_calls.index(call.click('button[type="submit"]')), self.page.method_calls.index(call.input('input[type="email"]', "second@example.invalid")))

    def test_latest_login_uses_solver_when_a_widget_is_present(self):
        with patch.object(app, "wait_turnstile_present", return_value=True), patch.object(app, "handle_turnstile", return_value=True) as solve:
            self.assertTrue(app.do_email_password_login(self.page, self.account))
        solve.assert_called_once_with(self.page)
        self.page.cdp.click_captcha.assert_not_called()

    def test_login_solver_failure_uses_upstream_fallback_and_leaves_confirmation_to_billings(self):
        with patch.object(app, "wait_turnstile_present", return_value=True), patch.object(app, "handle_turnstile", return_value=False):
            self.assertTrue(app.do_email_password_login(self.page, self.account))
        self.page.cdp.click_captcha.assert_called_once_with()
        self.assertEqual(self.page.click.call_count, 2)

    def test_login_input_error_becomes_a_failed_account_result(self):
        self.page.input.side_effect = RuntimeError("Form unavailable")
        self.assertFalse(app.do_email_password_login(self.page, self.account))
        self.assertEqual(self.page.click.call_count, 1)


class UpstreamSolverTests(unittest.TestCase):
    def setUp(self):
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        self.page = Mock()

    def test_native_click_applies_window_and_browser_toolbar_offsets(self):
        with patch.object(app, "js", side_effect=[{"cx": 130, "cy": 230}, "20|30|800|720"]), patch.object(
            app, "_xdotool_click"
        ) as click, patch.object(app.time, "sleep"):
            self.assertTrue(app._click_turnstile(self.page))
        click.assert_called_once_with(150, 340)

    def test_captcha_click_is_the_final_upstream_fallback(self):
        with patch.object(app, "_click_turnstile", return_value=False), patch.object(app, "js", return_value=None):
            self.assertTrue(app.click_turnstile(self.page))
        self.page.cdp.click_captcha.assert_called_once_with()

    def test_already_verified_token_does_not_click_the_challenge_again(self):
        with patch.object(app, "turnstile_token", return_value="verified"), patch.object(app, "click_turnstile") as click:
            self.assertTrue(app.solve_turnstile(self.page))
        click.assert_not_called()

    def test_upstream_solver_waits_before_clicking_and_accepts_a_later_token(self):
        clock = SimpleNamespace(now=0)
        def sleep(seconds):
            clock.now += seconds
        with patch.object(app.time, "time", side_effect=lambda: clock.now), patch.object(app.time, "sleep", side_effect=sleep), patch.object(
            app, "turnstile_token", side_effect=lambda sb: "verified" if clock.now >= 10 else ""
        ), patch.object(app, "click_turnstile") as click, patch.object(app.random, "uniform", return_value=0):
            self.assertTrue(app.solve_turnstile(self.page))
        click.assert_called_once_with(self.page)
        self.assertEqual(clock.now, 10)


if __name__ == "__main__":
    unittest.main()
