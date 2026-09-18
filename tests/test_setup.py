from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from config import DEFAULTS, load_settings, read_env, settings_from_values, validate_template
from admins import read_admins, update_admins
from setup import ask, build_proxy_url, persist_settings, save_env
from tools.manage_data import CONFIRMATION, backup_database, exclusive_bot_lock, reset_database


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.env = self.base / ".env"
        self.values = {**DEFAULTS, "BOT_TOKEN": "123456789:" + "a" * 35, "ADMIN_IDS": "123456"}

    def test_atomic_private_round_trip_preserves_literal_secrets(self):
        self.values["XUI_PASSWORD"] = "a'\\b\"$HOME # literal\nsecond line"
        self.values["UNRECOGNIZED_FUTURE_KEY"] = "keep me"
        save_env(self.env, self.values)
        self.assertEqual(read_env(self.env), self.values)
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)
        save_env(self.env, {"BRAND_NAME": "برند جدید"})
        updated = read_env(self.env)
        self.assertEqual(updated["XUI_PASSWORD"], self.values["XUI_PASSWORD"])
        self.assertEqual(updated["UNRECOGNIZED_FUTURE_KEY"], "keep me")
        self.assertEqual(list(self.base.glob('.env-*')), [])

    def test_blank_secret_keeps_current_without_showing_it(self):
        with patch("getpass.getpass", return_value="") as prompt:
            ask(self.values, "BOT_TOKEN", "توکن", secret=True)
        self.assertEqual(self.values["BOT_TOKEN"], "123456789:" + "a" * 35)
        self.assertNotIn(self.values["BOT_TOKEN"], prompt.call_args.args[0])

    def test_optional_clear_requires_explicit_dash(self):
        self.values["SUPPORT_USERNAME"] = "support_test"
        with patch("builtins.input", return_value=""):
            ask(self.values, "SUPPORT_USERNAME", "support", optional=True)
        self.assertEqual(self.values["SUPPORT_USERNAME"], "support_test")
        with patch("builtins.input", return_value="-"):
            ask(self.values, "SUPPORT_USERNAME", "support", optional=True)
        self.assertEqual(self.values["SUPPORT_USERNAME"], "")

    def test_environment_overrides_env_without_mutating_process(self):
        save_env(self.env, self.values)
        (self.base / "admins.json").write_text("[123456]")
        before = dict(os.environ)
        result = load_settings(env_file=self.env, environ={"BRAND_NAME": "دیگر"})
        self.assertEqual(result.brand_name, "دیگر")
        self.assertEqual(dict(os.environ), before)

    def test_admin_ids_are_numeric_positive_and_deduplicated(self):
        self.values["ADMIN_IDS"] = "123, 456،123"
        self.assertEqual(settings_from_values(self.values).admin_ids, (123, 456))
        for value in ("@name", "-1", "0", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings_from_values({**self.values, "ADMIN_IDS": value})

    def test_templates_reject_attributes_and_formatting(self):
        for value in ("{brand.__class__}", "{name!r}", "{brand:100000000}", "{unknown}", "{"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_template(value, {"brand", "name"})
        self.assertEqual(validate_template("{{ok}} {brand}", {"brand"}), "{{ok}} {brand}")

    def test_settings_defaults_relative_path_and_usernames(self):
        result = settings_from_values({**self.values, "SUPPORT_USERNAME": "https://t.me/help_test", "CHANNEL_USERNAME": "https://t.me/+ExampleInvite", "CARD_NUMBER": "۱۲۳۴-۵۶۷۸ ۹۰۱۲۳۴۵۶"}, base_dir=self.base)
        self.assertEqual(result.db_path, self.base / "data/bot.sqlite3")
        self.assertEqual(result.support_username, "help_test")
        self.assertEqual(result.channel_username, "+ExampleInvite")
        self.assertEqual(result.card_number, "1234567890123456")
        self.assertEqual(result.telegram_proxy_url, "")
        self.assertTrue(result.xui_verify_tls)

    def test_incomplete_panel_fails_before_save(self):
        with self.assertRaises(ValueError):
            settings_from_values({**self.values, "XUI_URL": "https://panel.example.com/path"})

    def test_env_is_never_shell_evaluated(self):
        self.env.write_text("BRAND_NAME=$(touch sentinel)\nWELCOME_TEXT='Hello ${brand}'\n", encoding="utf-8")
        self.assertEqual(read_env(self.env)["BRAND_NAME"], "$(touch sentinel)")
        self.assertEqual(read_env(self.env)["WELCOME_TEXT"], "Hello ${brand}")
        self.assertFalse((self.base / "sentinel").exists())

    def test_symlink_config_is_not_overwritten(self):
        original = self.base / "original"
        original.write_text("keep", encoding="utf-8")
        self.env.symlink_to(original)
        with self.assertRaises(ValueError):
            save_env(self.env, self.values)
        self.assertEqual(original.read_text(), "keep")

    def test_persist_settings_saves_env_and_admins_together(self):
        settings = persist_settings(self.env, self.values)
        self.assertEqual((123456,), settings.admin_ids)
        self.assertEqual((123456,), read_admins(self.base / "admins.json"))
        self.assertEqual(self.values, read_env(self.env))

    def test_admin_file_add_remove_guards(self):
        persist_settings(self.env, self.values)
        path = self.base / "admins.json"
        self.assertEqual((123456, 222), update_admins(path, add=222))
        with self.assertRaisesRegex(ValueError, "از قبل"):
            update_admins(path, add=222)
        with self.assertRaisesRegex(ValueError, "خودت"):
            update_admins(path, remove=123456, actor=123456)
        self.assertEqual((123456,), update_admins(path, remove=222, actor=123456))
        with self.assertRaisesRegex(ValueError, "حداقل"):
            update_admins(path, remove=123456, actor=999)

    def test_proxy_protocol_builder_handles_auth_and_ipv6(self):
        self.assertEqual(build_proxy_url("socks5h", "127.0.0.1", "۱۰۸۰"), "socks5h://127.0.0.1:1080")
        self.assertEqual(build_proxy_url("https", "2001:db8::1", "443", "user name", "p@ss"),
                         "https://user%20name:p%40ss@[2001:db8::1]:443")
        for values in (("ftp", "host", "80"), ("http", "bad/path", "80"), ("http", "host", "70000")):
            with self.subTest(values=values), self.assertRaises(ValueError):
                build_proxy_url(*values)


class DataManagementTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.db = self.base / "bot.sqlite3"
        self.backups = self.base / "backups"
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
            connection.execute("INSERT INTO users VALUES (1, 'example')")
            connection.commit()

    def test_backup_includes_committed_wal_data(self):
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("INSERT INTO users VALUES (2, 'new')")
            connection.commit()
            output = backup_database(self.db, self.backups)
            with closing(sqlite3.connect(output)) as saved:
                self.assertEqual(saved.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_reset_requires_exact_confirmation(self):
        with self.assertRaises(ValueError):
            reset_database(self.db, self.backups, "yes")
        self.assertTrue(self.db.exists())

    def test_reset_refuses_while_bot_lock_held(self):
        with exclusive_bot_lock(self.db), self.assertRaisesRegex(ValueError, "در حال اجرا"):
            reset_database(self.db, self.backups, CONFIRMATION)
        self.assertTrue(self.db.exists())

    def test_reset_keeps_private_env_and_backup(self):
        env = self.base / ".env"
        env.write_text("private settings", encoding="utf-8")
        output = reset_database(self.db, self.backups, CONFIRMATION)
        self.assertFalse(self.db.exists())
        self.assertEqual(env.read_text(), "private settings")
        with closing(sqlite3.connect(output)) as saved:
            self.assertEqual(saved.execute("SELECT name FROM users").fetchone()[0], "example")

    def test_failed_backup_prevents_reset(self):
        with patch("tools.manage_data.backup_database", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                reset_database(self.db, self.backups, CONFIRMATION)
        self.assertTrue(self.db.exists())


if __name__ == "__main__":
    unittest.main()
