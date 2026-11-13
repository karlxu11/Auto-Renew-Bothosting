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
const widget = rect => ({getBoundingClientRect: () => rect});
const widgets = (state.widgets || []).map(widget);
const shadowWidgets = (state.shadowWidgets || []).map(widget);
const parents = (state.tokenParents || []).map(widget);
parents.forEach((el, i) => el.parentElement = parents[i + 1] || null);
const context = vm.createContext({
  document: {
    body: {innerText: state.body || ''},
    querySelectorAll: selector => {
      if (selector === '[name="cf-turnstile-response"]' || selector === 'input[name="cf-turnstile-response"]')
        return (state.tokens || []).map(value => ({value}));
      if (selector.includes('iframe[src*=')) return widgets;
      if (selector === '*') return shadowWidgets.length ? [{shadowRoot: {
        querySelectorAll: selector => selector === '*' ? [] : shadowWidgets,
      }}] : [];
      return feedback;
    },
    querySelector: selector => {
      const tokens = state.tokens || [];
      if (tokens.length || parents.length) return {value: tokens[0] || '', parentElement: parents[0]};
      return selector.includes('iframe') ? (widgets[0] || null) : null;
    },
  },
  window: {screenX: 20, screenY: 30, outerHeight: 800, innerHeight: 720,
    turnstile: {getResponse: () => {
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
        self.cdp = SimpleNamespace(evaluate=self._run_script)

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
    def test_upstream_script_returns_viewport_coordinates_in_both_modes(self):
        for mode in ("webdriver", "cdp"):
            with self.subTest(mode=mode):
                browser = JavaScriptBrowser(mode, widgets=[{"x": 100, "y": 200, "width": 300, "height": 60}])
                self.assertEqual(app.js(browser, app.TS_COORDS_JS), {"cx": 130, "cy": 230})

    def test_widget_in_open_shadow_dom_remains_locatable(self):
        for mode in ("webdriver", "cdp"):
            with self.subTest(mode=mode):
                browser = JavaScriptBrowser(mode, shadowWidgets=[{"x": 100, "y": 200, "width": 300, "height": 60}])
                self.assertEqual(app.js(browser, app.TS_COORDS_JS), {"cx": 130, "cy": 230})

    def test_token_parent_can_locate_the_widget_when_iframe_is_unavailable(self):
        for mode in ("webdriver", "cdp"):
            with self.subTest(mode=mode):
                browser = JavaScriptBrowser(mode, tokenParents=[
                    {"x": 0, "y": 0, "width": 0, "height": 0},
                    {"x": 100, "y": 200, "width": 300, "height": 60},
                ])
                self.assertEqual(app.js(browser, app.TS_COORDS_JS), {"cx": 130, "cy": 230})

    def test_absent_or_unrendered_widgets_do_not_produce_click_coordinates(self):
        for mode in ("webdriver", "cdp"):
            for state in ({}, {"widgets": [{"x": 0, "y": 0, "width": 0, "height": 0}]}):
                with self.subTest(mode=mode, state=state):
                    self.assertIsNone(app.js(JavaScriptBrowser(mode, **state), app.TS_COORDS_JS))

    def test_token_in_response_field_is_detected_in_both_browser_modes(self):
        for mode in ("webdriver", "cdp"):
            with self.subTest(mode=mode):
                self.assertEqual(app.turnstile_token(JavaScriptBrowser(mode, tokens=["valid-token"])), "valid-token")

    def test_turnstile_api_can_supply_the_token(self):
        for mode in ("webdriver", "cdp"):
            with self.subTest(mode=mode):
                self.assertEqual(app.turnstile_token(JavaScriptBrowser(mode, apiToken="valid-token")), "valid-token")

    def test_pending_or_uninitialized_challenge_is_not_accepted(self):
        for mode in ("webdriver", "cdp"):
            for state in ({"tokens": [""]}, {"apiError": True}):
                with self.subTest(mode=mode, state=state):
                    self.assertEqual(app.turnstile_token(JavaScriptBrowser(mode, **state)), "")

    def test_turnstile_presence_does_not_require_a_completed_token(self):
        for mode in ("webdriver", "cdp"):
            with self.subTest(mode=mode):
                browser = JavaScriptBrowser(mode, tokens=[""])
                self.assertTrue(app.js(browser, app.TURNSTILE_EXISTS_JS))
                self.assertEqual(app.turnstile_token(browser), "")

    def test_browser_network_errors_remain_detectable(self):
        for mode in ("webdriver", "cdp"):
            with self.subTest(mode=mode):
                browser = JavaScriptBrowser(mode, body="This site can't be reached ERR_CONNECTION_RESET")
                self.assertEqual(app.browser_network_error(browser), "ERR_CONNECTION_RESET")


if __name__ == "__main__":
    unittest.main()
