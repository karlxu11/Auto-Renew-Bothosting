"""Execute the app's JavaScript under WebDriver and CDP semantics."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("browser_script_app", ROOT / "app.py")
app = importlib.util.module_from_spec(SPEC)
with patch.dict(os.environ, {"EMAIL": "test@example.invalid", "BOT_HOSTING_PASSWORD": "test-password"}), patch.dict(
    sys.modules, {"seleniumbase": SimpleNamespace(SB=None)}
):
    SPEC.loader.exec_module(app)


NODE_RUNNER = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const {script, state} = JSON.parse(fs.readFileSync(0, 'utf8'));
const feedback = (state.feedback || []).map(item => ({
  innerText: item.text,
  getClientRects: () => item.hidden ? [] : [{}],
  hidden: item.hidden,
}));
const context = vm.createContext({
  document: {
    body: {innerText: state.body || ''},
    querySelectorAll: selector => selector === '[name="cf-turnstile-response"]'
      ? (state.tokens || []).map(value => ({value})) : feedback,
  },
  window: {turnstile: {getResponse: () => {
    if (state.apiError) throw new Error('widget not ready');
    return state.apiToken || '';
  }}},
  getComputedStyle: el => ({display: el.hidden ? 'none' : 'block', visibility: 'visible'}),
});
// Polling runs repeatedly in the same page: declarations must not leak globally.
vm.runInContext(script, context);
const result = vm.runInContext(script, context);
process.stdout.write(JSON.stringify(result === undefined ? null : result));
"""


class JavaScriptBrowser:
    def __init__(self, mode, **state):
        self.mode = mode
        self.state = state

    def evaluate(self, expression):
        result = self._run_script(expression)
        if self.mode == "webdriver" and isinstance(result, (list, dict)):
            # BaseCase.evaluate falls back to WebDriver for object results.
            # Its return-prefix check also sees returns inside nested functions.
            script = expression if "return " in expression else "return " + expression
            return self.execute_script(script)
        return result

    def _run_script(self, expression):
        result = subprocess.run(
            [shutil.which("node"), "-e", NODE_RUNNER],
            input=json.dumps({"script": expression, "state": self.state}),
            text=True, capture_output=True, timeout=10,
        )
        if result.returncode:
            raise RuntimeError(result.stderr)
        return json.loads(result.stdout)

    def execute_script(self, script):
        if self.mode == "webdriver":
            # WebDriver executes a function body, where top-level return is legal.
            return self._run_script("(function () {\n" + script + "\n})()")
        # SeleniumBase's CDP adapter removes only a final line's leading return.
        expression = script.strip()
        lines = expression.split("\n")
        if lines[-1].strip().startswith("return "):
            expression = "\n".join(lines[:-1] + [lines[-1].strip()[len("return "):]])
        return self._run_script(expression)


@unittest.skipUnless(shutil.which("node"), "Node.js is required to execute the browser scripts")
class BrowserScriptTests(unittest.TestCase):
    def test_token_in_response_field_is_detected_in_both_browser_modes(self):
        for mode in ("webdriver", "cdp"):
            with self.subTest(mode=mode):
                self.assertTrue(app.turnstile_is_ready(JavaScriptBrowser(mode, tokens=["", "valid-token"])))

    def test_turnstile_api_can_supply_the_token(self):
        for mode in ("webdriver", "cdp"):
            with self.subTest(mode=mode):
                self.assertTrue(app.turnstile_is_ready(JavaScriptBrowser(mode, apiToken="valid-token")))

    def test_pending_or_uninitialized_challenge_is_not_accepted(self):
        for mode in ("webdriver", "cdp"):
            for state in ({"tokens": ["", " "]}, {"apiError": True}):
                with self.subTest(mode=mode, state=state):
                    self.assertFalse(app.turnstile_is_ready(JavaScriptBrowser(mode, **state)))

    def test_visible_feedback_is_returned_and_email_is_redacted(self):
        for mode in ("webdriver", "cdp"):
            with self.subTest(mode=mode):
                browser = JavaScriptBrowser(mode, feedback=[
                    {"text": "  Verification expired for test@example.invalid  "},
                    {"text": "Hidden error", "hidden": True},
                    {"text": "Try again"},
                ])
                self.assertEqual(app.get_login_feedback(browser), [
                    "Verification expired for [email]", "Try again",
                ])

    def test_browser_network_errors_remain_detectable(self):
        for mode in ("webdriver", "cdp"):
            with self.subTest(mode=mode):
                browser = JavaScriptBrowser(mode, body="This site can't be reached ERR_CONNECTION_RESET")
                self.assertEqual(app.browser_network_error(browser), "ERR_CONNECTION_RESET")


if __name__ == "__main__":
    unittest.main()
