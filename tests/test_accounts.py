import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import accounts


CONFIG = [
    {"id": "account_1", "email": "first@example.invalid", "password": "first-test-password"},
    {"id": "custom-id", "name": "Second", "email": "second@example.invalid", "password": "second-test-password"},
    {"id": "123", "email": "third@example.invalid", "password": "third-test-password"},
]
ROOT = Path(__file__).resolve().parents[1]


class AccountSelectionTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"BOT_HOSTING_ACCOUNTS": json.dumps(CONFIG)}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.accounts = accounts.load_accounts()

    def test_single_and_multiple_manual_account_ids(self):
        for raw, expected in [
            ("custom-id", ["custom-id"]),
            ("custom-id,account_1", ["custom-id", "account_1"]),
            ("  custom-id , account_1  ", ["custom-id", "account_1"]),
            ("custom-id\naccount_1", ["custom-id", "account_1"]),
            ("123", ["123"]),
        ]:
            with self.subTest(raw=raw):
                result = accounts.select_accounts(self.accounts, raw)
                self.assertEqual([item["id"] for item in result], expected)

    def test_worker_json_selection_preserves_order_and_deduplicates(self):
        selected = accounts.select_accounts(self.accounts, '["custom-id", "account_1", "custom-id"]')
        self.assertEqual([item["id"] for item in selected], ["custom-id", "account_1"])

    def test_blank_and_legacy_null_select_all_but_empty_json_selects_none(self):
        for raw in ["", "  ", "null"]:
            with self.subTest(raw=raw):
                self.assertEqual(accounts.select_accounts(self.accounts, raw), self.accounts)
        self.assertEqual(accounts.select_accounts(self.accounts, "[]"), [])

    def test_invalid_selection_never_falls_back_to_all_accounts(self):
        for raw in ["missing", "account_1,missing", '["missing"]', "[broken", "{}", '"account_1"', "[123]", '[""]', "account_1;echo", ",", "account_1,"]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                accounts.select_accounts(self.accounts, raw)

    def test_environment_selection_is_used_by_the_app(self):
        os.environ["SELECTED_ACCOUNT_IDS"] = '["custom-id"]'
        self.assertEqual([item["id"] for item in accounts.select_accounts(self.accounts)], ["custom-id"])

    def test_single_account_configuration_remains_compatible(self):
        with patch.dict(os.environ, {"EMAIL": "single@example.invalid", "BOT_HOSTING_PASSWORD": "single-test-password"}, clear=True):
            loaded = accounts.load_accounts()
        self.assertEqual(loaded[0]["id"], "account_1")
        self.assertEqual(loaded[0]["password"], "single-test-password")

    def test_invalid_account_configuration_is_rejected(self):
        for raw in ["bad-json", "{}", "[]", '[{}]', '[1]', json.dumps([CONFIG[0], CONFIG[0]])]:
            with self.subTest(raw=raw), patch.dict(os.environ, {"BOT_HOSTING_ACCOUNTS": raw}), self.assertRaises(ValueError):
                accounts.load_accounts()

    def test_preflight_exports_exact_ids_and_summary_without_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            env_file = Path(folder) / "env"
            summary_file = Path(folder) / "summary"
            environment = dict(os.environ, SELECTED_ACCOUNT_IDS="custom-id", GITHUB_ENV=str(env_file), GITHUB_STEP_SUMMARY=str(summary_file), GITHUB_RUN_ATTEMPT="2")
            result = subprocess.run([sys.executable, str(ROOT / "accounts.py")], env=environment, capture_output=True, text=True, check=True)
            self.assertEqual(env_file.read_text(), 'SELECTED_ACCOUNT_IDS=["custom-id"]\n')
            summary = summary_file.read_text()
            self.assertIn("`custom-id`", summary)
            self.assertIn("运行次数：2", summary)
            self.assertIn("Re-run jobs", summary)
            self.assertIn("custom-id", result.stdout)
            self.assertNotIn("account_1", summary)
            output = result.stdout + result.stderr + summary + env_file.read_text()
            for account in CONFIG:
                self.assertNotIn(account["email"], output)
                self.assertNotIn(account["password"], output)

    def test_invalid_preflight_does_not_export_a_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            env_file = Path(folder) / "env"
            environment = dict(os.environ, SELECTED_ACCOUNT_IDS="missing", GITHUB_ENV=str(env_file))
            result = subprocess.run([sys.executable, str(ROOT / "accounts.py")], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("未知账号 ID", result.stderr)
            self.assertFalse(env_file.exists())

    def test_renewal_app_processes_only_the_exported_account(self):
        spec = importlib.util.spec_from_file_location("renewal_app_for_test", ROOT / "app.py")
        app = importlib.util.module_from_spec(spec)
        environment = {
            "SELECTED_ACCOUNT_IDS": '["custom-id"]',
            "SCHEDULER_URL": "https://scheduler.example.invalid",
            "SCHEDULER_AUTH_KEY": "test-scheduler-key",
            "IS_PROXY": "false",
        }
        with patch.dict(os.environ, environment), patch.dict(sys.modules, {"seleniumbase": SimpleNamespace(SB=None)}):
            spec.loader.exec_module(app)
            with patch.object(app, "renew_account", return_value={"status": "not_due"}) as renew, patch.object(
                app, "report_account_schedule"
            ) as report, contextlib.redirect_stdout(io.StringIO()):
                app.main()
        self.assertEqual(renew.call_count, 1)
        self.assertEqual(renew.call_args.args[0]["id"], "custom-id")
        self.assertFalse(renew.call_args.args[3])
        self.assertEqual(report.call_args.args[0]["id"], "custom-id")


if __name__ == "__main__":
    unittest.main()
