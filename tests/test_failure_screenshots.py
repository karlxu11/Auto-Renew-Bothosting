import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("failure_screenshot_app", ROOT / "app.py")
app = importlib.util.module_from_spec(SPEC)
with patch.dict(os.environ, {"EMAIL": "test@example.invalid", "BOT_HOSTING_PASSWORD": "test-password"}), patch.dict(
    sys.modules, {"seleniumbase": SimpleNamespace(SB=None)}
):
    SPEC.loader.exec_module(app)


class Browser:
    def __init__(self, events):
        self.events = events
        self.closed = False
        self.page = "last failure page"
        self.redaction_error = False
        self.capture_error = False
        self.scripts = []

    def __enter__(self):
        self.events.append("enter")
        return self

    def __exit__(self, *args):
        self.closed = True
        self.events.append("exit")

    def execute_script(self, script):
        if self.closed:
            raise RuntimeError("browser already closed")
        self.scripts.append(script)
        if "document.body.innerText" in script:
            return self.page
        if "setProperty" in script:
            self.events.append("mask")
            if self.redaction_error:
                raise RuntimeError("redaction unavailable")
        else:
            self.events.append("restore")

    def save_screenshot(self, path):
        if self.closed:
            raise RuntimeError("browser already closed")
        self.events.append("capture")
        if self.capture_error:
            raise RuntimeError("screenshot unavailable")
        Path(path).write_bytes(self.page.encode())


class FailureScreenshotTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        previous_cwd = os.getcwd()
        os.chdir(folder.name)
        self.addCleanup(os.chdir, previous_cwd)
        self.events = []
        self.browser = Browser(self.events)
        self.account = {"id": "account_3", "name": "Test account", "email": "test@example.invalid", "password": "test-password"}
        self.sent = []
        self.photo_ok = True
        self.output = io.StringIO()
        for manager in (
            patch.object(app, "SB", return_value=self.browser),
            patch.object(app, "get_current_ip", return_value="192.0.2.1"),
            patch.object(app, "TG_BOT_TOKEN", "test-token"),
            patch.object(app, "TG_CHAT_ID", "test-chat"),
            patch.object(app.requests, "post", side_effect=self.post),
            contextlib.redirect_stdout(self.output),
        ):
            manager.__enter__()
            self.addCleanup(manager.__exit__, None, None, None)
        self.login = self.enter(patch.object(app, "do_email_password_login", return_value=True))
        self.billings = self.enter(patch.object(app, "open_billings_page", return_value=True))
        self.renew = self.enter(patch.object(app, "renew_with_retry", return_value={"status": "not_due", "extra": "tomorrow"}))

    def enter(self, manager):
        result = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return result

    def post(self, url, **kwargs):
        photo = url.endswith("/sendPhoto")
        if photo:
            filename, file, _ = kwargs["files"]["photo"]
            if filename.startswith("failure_"):
                self.assertEqual(self.events[-1], "restore")
            self.sent.append({"type": "photo", "filename": filename, "image": file.read(), "text": kwargs["data"]["caption"]})
        else:
            self.sent.append({"type": "text", "text": kwargs["json"]["text"]})
        self.events.append("telegram")
        ok = self.photo_ok if photo else True
        return SimpleNamespace(ok=ok, status_code=200 if ok else 400, raise_for_status=lambda: None)

    def run_account(self, account=None):
        return app.renew_account(account or self.account, {}, "", False)

    def assert_failure_photo(self, result, step):
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_step"], step)
        self.assertTrue(Path(result["screenshot"]).is_file())
        self.assertEqual(self.sent[0]["type"], "photo")
        self.assertEqual(self.sent[0]["filename"], Path(result["screenshot"]).name)
        self.assertIn("account_3（karlzhe）", self.sent[0]["text"])
        self.assertTrue(self.browser.closed)
        self.assertLess(self.events.index("telegram"), self.events.index("exit"))
        masking = next(script for script in self.browser.scripts if "setProperty" in script)
        self.assertIn('input[type="password"]', masking)
        self.assertIn('input[type="email"]', masking)

    def test_login_failure_sends_final_redacted_page_before_browser_closes(self):
        self.login.return_value = False
        result = self.run_account()
        self.assert_failure_photo(result, "login")
        self.billings.assert_not_called()
        self.renew.assert_not_called()

    def test_billings_failure_sends_the_failed_navigation_page(self):
        self.billings.return_value = False
        result = self.run_account()
        self.assert_failure_photo(result, "billings")
        self.assertIn("打开账单页", self.sent[0]["text"])
        self.renew.assert_not_called()

    def test_final_renewal_failure_captures_current_page_instead_of_earlier_attempt(self):
        Path("earlier-attempt.png").write_bytes(b"old attempt")
        self.renew.return_value = {"status": "failed", "error": "retry failed", "screenshot": "earlier-attempt.png"}
        self.browser.page = "last page after failed retry"
        result = self.run_account()
        self.assert_failure_photo(result, "renewal")
        self.assertEqual(self.sent[0]["image"], b"last page after failed retry")
        self.assertNotEqual(result["screenshot"], "earlier-attempt.png")

    def test_unexpected_browser_error_is_captured_inside_the_browser_context(self):
        self.renew.side_effect = RuntimeError("navigation timed out")
        result = self.run_account()
        self.assert_failure_photo(result, "renewal")
        self.assertIn("navigation timed out", self.sent[0]["text"])

    def test_redaction_failure_sends_text_without_capturing_unmasked_page(self):
        self.login.return_value = False
        self.browser.redaction_error = True
        result = self.run_account()
        self.assertEqual(result["screenshot"], "")
        self.assertNotIn("capture", self.events)
        self.assertIn("restore", self.events)
        self.assertEqual([item["type"] for item in self.sent], ["text"])
        self.assertIn("无法截图", self.sent[0]["text"])

    def test_capture_failure_restores_styles_and_does_not_send_an_old_file(self):
        self.login.return_value = False
        self.browser.capture_error = True
        Path("failure_account_3_login_old.png").write_bytes(b"stale image")
        result = self.run_account()
        self.assertEqual(result["screenshot"], "")
        self.assertEqual([item["type"] for item in self.sent], ["text"])
        self.assertEqual(self.events, ["enter", "mask", "capture", "restore", "telegram", "exit"])

    def test_telegram_photo_error_falls_back_to_text_and_keeps_failed_status(self):
        self.login.return_value = False
        self.photo_ok = False
        result = self.run_account()
        self.assertEqual(result["status"], "failed")
        self.assertEqual([item["type"] for item in self.sent], ["photo", "text"])

    def test_failed_accounts_have_separate_current_screenshots(self):
        first = self.browser
        second = Browser(self.events)
        first.page = "first account failed page"
        second.page = "second account failed page"
        app.SB.side_effect = [first, second]
        self.login.return_value = False
        result1 = self.run_account()
        result2 = self.run_account({**self.account, "id": "account_1"})
        self.assertNotEqual(result1["screenshot"], result2["screenshot"])
        self.assertIn("account_3", result1["screenshot"])
        self.assertIn("account_1", result2["screenshot"])
        self.assertEqual([item["image"] for item in self.sent], [b"first account failed page", b"second account failed page"])

    def test_success_keeps_the_existing_result_photo(self):
        Path("success.png").write_bytes(b"successful renewal")
        self.renew.return_value = {"status": "success", "extra": "renewed", "screenshot": "success.png"}
        result = self.run_account()
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.sent[0]["filename"], "success.png")
        self.assertIn("续期成功", self.sent[0]["text"])
        self.assertEqual(list(Path.cwd().glob("failure_*.png")), [])

    def test_not_due_sends_the_existing_text_without_a_failure_photo(self):
        result = self.run_account()
        self.assertEqual(result["status"], "not_due")
        self.assertEqual([item["type"] for item in self.sent], ["text"])
        self.assertIn("未到续期时间", self.sent[0]["text"])
        self.assertNotIn("capture", self.events)

    def test_unrecognized_renewal_result_is_reported_as_failure(self):
        self.renew.return_value = {"status": "unexpected"}
        result = self.run_account()
        self.assert_failure_photo(result, "renewal")

    def test_missing_telegram_config_still_saves_the_failure_for_artifacts(self):
        self.login.return_value = False
        with patch.object(app, "TG_BOT_TOKEN", ""):
            result = self.run_account()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(Path(result["screenshot"]).is_file())
        self.assertEqual(self.sent, [])

    def test_proxy_failure_closes_the_old_browser_before_direct_retry_and_notifies_only_final_result(self):
        first = self.browser
        first.page = "This site can't be reached ERR_SOCKS_CONNECTION_FAILED"
        second = Browser(self.events)
        launches = []

        def launch(**kwargs):
            launches.append(kwargs)
            if len(launches) == 1:
                return first
            self.assertTrue(first.closed)
            return second

        app.SB.side_effect = launch
        self.login.side_effect = [False, True]
        with patch.object(app, "proxy_is_available", return_value=True), patch.object(app, "vpngate_connection_snapshot", return_value=None):
            result = app.renew_account(self.account, {"uc": True, "proxy": "socks5://127.0.0.1:10080"}, "socks5://127.0.0.1:10080", True)
        self.assertEqual(result["status"], "not_due")
        self.assertTrue(result["direct_fallback"])
        self.assertIn("proxy", launches[0])
        self.assertNotIn("proxy", launches[1])
        self.assertTrue(second.closed)
        self.assertEqual([item["type"] for item in self.sent], ["text"])
        self.assertIn("未到续期时间", self.sent[0]["text"])
        self.assertEqual(len(list(Path.cwd().glob("failure_account_3_login_*.png"))), 1)


if __name__ == "__main__":
    unittest.main()
