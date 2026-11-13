"""Verify the latest upstream renewal flow and its scheduler result adapter."""

import contextlib
import importlib.util
import io
import os
from datetime import datetime, timedelta, timezone
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


OUTER = 'button:contains("Renew free plan")'
SUBMIT = 'div[role="dialog"] button.btn-primary'
CLASS_OUTER = '[class*="btn btn-primary btn-sm"]'


class BillingPage:
    def __init__(self, button="Renew free plan", result="Expires 19 Nov 2026. Renewal cooldown 47:59:59", outer=OUTER):
        self.button = button
        self.outer = outer
        self.result = result
        self.clicks = []
        self.sleeps = []
        self.submitted = False
        self.click_error = ""
        self.text_error = False
        self.cdp = SimpleNamespace(click_captcha=Mock())

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def is_element_present(self, selector):
        return False

    def is_element_visible(self, selector):
        if selector.startswith("type:"):
            raise ValueError("Invalid selector")
        return selector == self.outer and self.button is not None

    def get_text(self, selector):
        return self.button

    def get_page_source(self):
        return self.result if self.submitted else "Expires 15 Nov 2026. " + (self.button or "")

    def get_page_text(self):
        if self.text_error:
            raise RuntimeError("Visible text temporarily unavailable")
        return self.get_page_source()

    def click(self, selector, **kwargs):
        self.clicks.append(selector)
        if selector == self.click_error:
            raise RuntimeError("Click failed")
        if selector == SUBMIT:
            self.submitted = True


class RenewalTests(unittest.TestCase):
    def setUp(self):
        for manager in (
            contextlib.redirect_stdout(io.StringIO()),
            patch.object(app.time, "sleep"),
            patch.object(app, "wait_turnstile_present", return_value=False),
            patch.object(app, "save_renewal_screenshot", return_value="renew_result_1.png"),
        ):
            manager.__enter__()
            self.addCleanup(manager.__exit__, None, None, None)

    def test_latest_modal_selector_and_countdown_confirm_success(self):
        page = BillingPage()
        result = app.renew_current_page(page)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["countdown"], "47:59:59")
        self.assertEqual(result["expiry_date"], "19 Nov 2026")
        self.assertEqual(result["screenshot"], "renew_result_1.png")
        self.assertEqual(page.clicks, [OUTER, SUBMIT])
        self.assertEqual(page.sleeps, [3, 2, 15, 6])
        app.time.sleep.assert_called_once_with(5)

    def test_updated_expiry_confirms_success_without_a_countdown_as_upstream_does(self):
        result = app.renew_current_page(BillingPage(result="Expires 19 Nov 2026"))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["expiry_date"], "19 Nov 2026")
        self.assertNotIn("countdown", result)

    def test_unchanged_expiry_with_no_countdown_remains_a_failed_unknown_result(self):
        page = BillingPage(result="Expires 15 Nov 2026")
        result = app.renew_current_page(page)
        self.assertEqual(result["status"], "failed")
        self.assertIn("到期日期未变化", result["error"])
        self.assertEqual(page.clicks.count(SUBMIT), 1)
        app.save_renewal_screenshot.assert_not_called()

    def test_cooldown_does_not_click_any_renewal_button(self):
        page = BillingPage(button="Renewal cooldown 02:41:56", outer=CLASS_OUTER)
        result = app.renew_current_page(page)
        self.assertEqual(result["status"], "not_due")
        self.assertEqual(result["countdown"], "02:41:56")
        self.assertEqual(page.clicks, [])
        app.save_renewal_screenshot.assert_not_called()

    def test_renew_in_cooldown_is_supported(self):
        self.assertEqual(app.renew_current_page(BillingPage(button="Renew in 12:34:56"))["countdown"], "12:34:56")

    def test_upstream_class_selector_is_a_fallback_for_the_outer_button(self):
        page = BillingPage(outer=CLASS_OUTER)
        self.assertEqual(app.renew_current_page(page)["status"], "success")
        self.assertEqual(page.clicks[0], CLASS_OUTER)

    def test_missing_button_maps_unknown_state_to_scheduler_failure(self):
        page = BillingPage(button=None)
        self.assertEqual(app.renew_current_page(page)["status"], "failed")
        self.assertEqual(page.clicks, [])

    def test_outer_click_failure_does_not_retry_the_renewal(self):
        page = BillingPage()
        page.click_error = OUTER
        self.assertEqual(app.renew_current_page(page)["status"], "failed")
        self.assertEqual(page.clicks, [OUTER])

    def test_failed_submit_does_not_claim_success_when_the_page_has_not_changed(self):
        page = BillingPage()
        page.click_error = SUBMIT
        self.assertEqual(app.renew_current_page(page)["status"], "failed")
        self.assertEqual(page.clicks.count(SUBMIT), 1)

    def test_page_source_is_used_if_visible_text_reading_fails(self):
        page = BillingPage()
        page.text_error = True
        self.assertEqual(app.renew_current_page(page)["status"], "success")

    def test_present_challenge_uses_upstream_solver(self):
        page = BillingPage()
        with patch.object(app, "wait_turnstile_present", return_value=True), patch.object(app, "handle_turnstile", return_value=True) as solve:
            self.assertEqual(app.renew_current_page(page)["status"], "success")
        solve.assert_called_once_with(page)

    def test_failed_solver_rechecks_the_token_after_upstream_fallback(self):
        page = BillingPage()
        with patch.object(app, "wait_turnstile_present", return_value=True), patch.object(
            app, "handle_turnstile", return_value=False
        ), patch.object(app, "turnstile_token", return_value="fallback-token"):
            self.assertEqual(app.renew_current_page(page)["status"], "success")
        page.cdp.click_captcha.assert_called_once_with()

    def test_failed_challenge_never_clicks_the_modal_submit(self):
        page = BillingPage()
        with patch.object(app, "wait_turnstile_present", return_value=True), patch.object(
            app, "handle_turnstile", return_value=False
        ), patch.object(app, "turnstile_token", return_value=""):
            self.assertEqual(app.renew_current_page(page)["status"], "failed")
        self.assertNotIn(SUBMIT, page.clicks)

    def test_latest_upstream_date_formats_are_extracted(self):
        for text, expected in (
            ("Expires 19 November 2026", "19 November 2026"),
            ("Expires 11/19/2026", "2026/11/19"),
            ("Expires 2026-11-19", "2026-11-19"),
        ):
            with self.subTest(text=text):
                self.assertEqual(app.extract_expiry_date(text), expected)

    def test_one_failed_account_does_not_prevent_reporting_the_rest(self):
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


class BillingNavigationTests(unittest.TestCase):
    def test_current_upstream_billings_page_is_used(self):
        page = Mock()
        page.get_current_url.return_value = "https://bot-hosting.net/dash/billings"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(app.open_billings_page(page))
        page.open.assert_called_once_with("https://bot-hosting.net/dash/billings")

    def test_login_or_error_redirect_is_not_accepted(self):
        for url in ("https://bot-hosting.net/login", "https://bot-hosting.net/dash/billings?error=session_expired"):
            page = Mock()
            page.get_current_url.return_value = url
            with self.subTest(url=url), contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(app.open_billings_page(page))


class SchedulerAdapterTests(unittest.TestCase):
    def test_upstream_results_keep_the_existing_per_account_schedule_contract(self):
        now = datetime(2026, 11, 13, 8, 0, tzinfo=timezone.utc)
        account = {"id": "account_2", "name": "Second account"}
        for result, delay in (
            ({"status": "success", "countdown": "47:59:59"}, timedelta(seconds=47 * 3600 + 59 * 60 + 59 + 120)),
            ({"status": "not_due", "countdown": "02:41:56"}, timedelta(seconds=2 * 3600 + 41 * 60 + 56 + 120)),
            ({"status": "success", "expiry_date": "19 Nov 2026"}, timedelta(hours=24)),
            ({"status": "failed", "error": "unknown upstream result"}, timedelta(hours=6)),
        ):
            with self.subTest(result=result), patch.object(app, "datetime") as clock, patch.object(
                app.requests, "post"
            ) as post, patch.object(app, "SCHEDULER_URL", "https://scheduler.example.invalid"), patch.object(
                app, "SCHEDULER_AUTH_KEY", "test-key"
            ), contextlib.redirect_stdout(io.StringIO()):
                clock.now.return_value = now
                app.report_account_schedule(account, result)
                payload = post.call_args.kwargs["json"]
                self.assertEqual(payload["account_id"], "account_2")
                self.assertEqual(payload["status"], result["status"])
                self.assertEqual(payload["next_run_at"], (now + delay).isoformat().replace("+00:00", "Z"))
                self.assertEqual(post.call_args.args[0], "https://scheduler.example.invalid/api/schedule")


if __name__ == "__main__":
    unittest.main()
