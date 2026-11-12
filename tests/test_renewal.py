import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("renewal_app_regression", ROOT / "app.py")
app = importlib.util.module_from_spec(SPEC)
with patch.dict(os.environ, {"EMAIL": "test@example.invalid", "BOT_HOSTING_PASSWORD": "test-password"}), patch.dict(
    sys.modules, {"seleniumbase": SimpleNamespace(SB=None)}
):
    SPEC.loader.exec_module(app)


SUBMIT = 'button:contains("Renew for 4 days")'


class FakeClock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class BillingPage:
    """Model an iframe challenge invisible to the outer HTML and a disabled no-op button."""

    def __init__(self, clock, token_after=0, enabled_after=0, result_after=0, verification_delay=None):
        self.clock = clock
        self.token_after = token_after
        self.enabled_after = enabled_after
        self.result_after = result_after
        self.verification_delay = verification_delay
        self.aria_disabled = False
        self.submitted_at = None
        self.clicks = []
        self.captcha_clicks = 0
        self.feedback = []

    def sleep(self, seconds):
        self.clock.sleep(seconds)

    def evaluate(self, script):
        if "cf-turnstile-response" in script:
            return self.clock.now >= self.token_after
        if "JSON.stringify" in script:
            return json.dumps(self.feedback)
        return self.feedback

    def get_page_source(self):
        if self.submitted_at is not None and self.clock.now - self.submitted_at >= self.result_after:
            return "<button>Renewal cooldown 47:59:59</button>"
        return '<button>Renew free plan</button><iframe src="https://challenges.cloudflare.com/"></iframe>'

    def is_element_visible(self, selector):
        return True

    def is_element_enabled(self, selector):
        return self.clock.now >= self.enabled_after

    def get_attribute(self, selector, attribute, **kwargs):
        return "true" if self.aria_disabled else None

    def get_text(self, selector):
        if "Renewal cooldown" in self.get_page_source():
            return "Renewal cooldown 47:59:59"
        return "Renew free plan"

    def wait_for_element_visible(self, selector, **kwargs):
        pass

    def wait_for_element_clickable(self, selector, **kwargs):
        if not self.is_element_enabled(selector):
            raise RuntimeError("button disabled")

    def uc_gui_click_captcha(self):
        self.captcha_clicks += 1
        if self.verification_delay is not None:
            # 重复点击会重启验证，用于模拟长时间停留在 Verifying 的组件。
            self.token_after = self.enabled_after = self.clock.now + self.verification_delay

    def click(self, selector, **kwargs):
        self.clicks.append(selector)
        if selector == SUBMIT and self.is_element_enabled(selector):
            self.submitted_at = self.clock.now


class BillingNavigationTests(unittest.TestCase):
    def setUp(self):
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def browser(self, url):
        page = Mock()
        page.get_current_url.return_value = url
        page.get_title.return_value = "Billing | Bot-Hosting"
        return page

    def test_current_billings_page_from_failed_run_is_accepted(self):
        page = self.browser("https://bot-hosting.net/dash/billings")
        self.assertTrue(app.open_billings_page(page))
        page.open.assert_called_once_with("https://bot-hosting.net/dash/billings")

    def test_legacy_billings_route_remains_compatible(self):
        self.assertTrue(app.open_billings_page(self.browser("https://bot-hosting.net/a/billings")))

    def test_login_error_and_unrelated_destinations_are_rejected(self):
        for url in (
            "https://bot-hosting.net/login?next=/a/billings",
            "https://bot-hosting.net/dash/billings?error=session_expired",
            "https://bot-hosting.net/dash",
            "https://bot-hosting.net/a/billings-unavailable",
            "https://example.invalid/a/billings",
        ):
            with self.subTest(url=url):
                self.assertFalse(app.open_billings_page(self.browser(url)))

    def test_logged_billing_page_reaches_cooldown_check_without_renewing(self):
        page = self.browser("https://bot-hosting.net/dash/billings")
        page.is_element_visible.return_value = False
        page.get_page_source.return_value = "<span>Renewal cooldown 02:41:56</span>"
        self.assertTrue(app.open_billings_page(page))
        result = app.renew_with_retry(page)
        self.assertEqual(result["status"], "not_due")
        self.assertEqual(result["countdown"], "02:41:56")
        page.click.assert_not_called()
        self.assertEqual(page.open.call_count, 1)


class RenewalTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.output = io.StringIO()
        for manager in (
            patch.object(app, "time", self.clock),
            patch.object(app, "save_renewal_screenshot", side_effect=lambda sb, attempt: f"renew_result_{attempt}.png"),
            contextlib.redirect_stdout(self.output),
        ):
            manager.__enter__()
            self.addCleanup(manager.__exit__, None, None, None)

    def test_absent_outer_challenge_text_does_not_allow_submission(self):
        page = BillingPage(self.clock, token_after=float("inf"))
        result = app.attempt_renewal(page, 1)
        self.assertEqual(result["status"], "failed")
        self.assertIn("未提交续期", result["error"])
        self.assertEqual(result["screenshot"], "renew_result_1.png")
        self.assertNotIn(SUBMIT, page.clicks)
        self.assertEqual(page.captcha_clicks, 3)

    def test_token_alone_does_not_allow_clicking_disabled_button(self):
        page = BillingPage(self.clock, enabled_after=float("inf"))
        result = app.attempt_renewal(page, 1)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn(SUBMIT, page.clicks)

    def test_aria_disabled_is_respected(self):
        page = BillingPage(self.clock)
        page.aria_disabled = True
        self.assertFalse(app.wait_for_turnstile_pass(page, timeout=3, submit_selector=SUBMIT))

    def test_waits_for_token_and_button_then_delayed_result(self):
        page = BillingPage(self.clock, token_after=3, enabled_after=6, result_after=18)
        result = app.attempt_renewal(page, 1)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["countdown"], "47:59:59")
        self.assertEqual(page.submitted_at, 6)
        self.assertEqual(self.clock.now, 24)
        self.assertEqual(page.clicks.count(SUBMIT), 1)

    def test_verification_can_succeed_on_a_later_checkbox_attempt(self):
        page = BillingPage(self.clock, token_after=75, enabled_after=75)
        self.assertEqual(app.attempt_renewal(page, 1)["status"], "success")
        self.assertEqual(page.captcha_clicks, 2)
        self.assertEqual(page.clicks.count(SUBMIT), 1)

    def test_slow_verification_is_not_restarted_before_it_completes(self):
        page = BillingPage(
            self.clock, token_after=float("inf"), enabled_after=float("inf"), verification_delay=50,
        )
        self.assertEqual(app.attempt_renewal(page, 1)["status"], "success")
        self.assertEqual(page.captcha_clicks, 1)
        self.assertEqual(page.clicks.count(SUBMIT), 1)

    def test_automatically_verified_popup_does_not_click_the_challenge_again(self):
        page = BillingPage(self.clock, token_after=3, enabled_after=4)
        self.assertEqual(app.attempt_renewal(page, 1)["status"], "success")
        self.assertEqual(page.captcha_clicks, 0)
        self.assertEqual(page.clicks.count(SUBMIT), 1)

    def test_verification_during_retry_pause_avoids_a_second_checkbox_click(self):
        page = BillingPage(self.clock, token_after=68, enabled_after=68)
        self.assertEqual(app.attempt_renewal(page, 1)["status"], "success")
        self.assertEqual(page.captcha_clicks, 1)
        self.assertEqual(page.clicks.count(SUBMIT), 1)

    def test_verification_is_checked_at_the_timeout_boundary(self):
        page = BillingPage(self.clock, token_after=3, enabled_after=3)
        self.assertTrue(app.wait_for_turnstile_pass(page, timeout=3, submit_selector=SUBMIT))

    def test_click_without_confirmed_result_is_not_success(self):
        page = BillingPage(self.clock, result_after=float("inf"))
        page.feedback = ["Verification expired"]
        result = app.attempt_renewal(page, 1)
        self.assertEqual(result["status"], "failed")
        self.assertIn("45 秒", result["error"])
        self.assertEqual(self.clock.now - page.submitted_at, 45)
        self.assertEqual(page.clicks.count(SUBMIT), 1)
        self.assertIn("Verification expired", self.output.getvalue())

    def test_refresh_can_confirm_previous_submission_without_submitting_again(self):
        page = BillingPage(self.clock, result_after=50)

        def refresh(sb):
            self.clock.sleep(8)
            return True

        with patch.object(app, "open_billings_page", side_effect=refresh):
            result = app.renew_with_retry(page)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["countdown"], "47:59:59")
        self.assertEqual(page.clicks.count(SUBMIT), 1)

    def test_retry_reloads_and_gets_a_fresh_challenge(self):
        page = BillingPage(self.clock, token_after=float("inf"))

        def refresh(sb):
            page.token_after = self.clock.now + 2
            page.enabled_after = self.clock.now + 2
            return True

        with patch.object(app, "open_billings_page", side_effect=refresh) as reload:
            result = app.renew_with_retry(page)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["screenshot"], "renew_result_2.png")
        self.assertEqual(page.clicks.count(SUBMIT), 1)
        reload.assert_called_once()

    def test_persistent_verification_failure_stops_after_two_attempts(self):
        page = BillingPage(self.clock, token_after=float("inf"))
        with patch.object(app, "open_billings_page", return_value=True) as reload:
            result = app.renew_with_retry(page)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["screenshot"], "renew_result_2.png")
        self.assertNotIn(SUBMIT, page.clicks)
        self.assertEqual(page.captcha_clicks, 6)
        reload.assert_called_once()

    def test_login_also_requires_a_positive_token(self):
        page = BillingPage(self.clock, token_after=2)
        self.assertTrue(app.wait_for_turnstile_pass(page, timeout=3))
        self.assertEqual(self.clock.now, 2)

    def test_failed_account_marks_action_failed_after_processing_and_reporting_all_accounts(self):
        accounts = [{"id": "first"}, {"id": "second"}]
        results = [{"status": "failed", "error": "verification failed"}, {"status": "not_due"}]
        with patch.object(app, "load_accounts", return_value=accounts), patch.object(
            app, "select_accounts", side_effect=lambda items: items
        ), patch.object(app, "SCHEDULER_URL", "https://scheduler.example.invalid"), patch.object(
            app, "SCHEDULER_AUTH_KEY", "test-key"
        ), patch.object(app, "renew_account", side_effect=results) as renew, patch.object(
            app, "report_account_schedule"
        ) as report, self.assertRaises(SystemExit) as raised:
            app.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(renew.call_count, 2)
        self.assertEqual(report.call_count, 2)
        self.assertEqual(report.call_args_list[0].args, (accounts[0], results[0]))
        self.assertEqual(report.call_args_list[1].args, (accounts[1], results[1]))


if __name__ == "__main__":
    unittest.main()
