#!/usr/bin/env python3
"""Interactive SSH configuration; standard library only, with no network calls."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import quote

from config import DEFAULTS, ENV_FILE, read_env, settings_from_values
from admins import read_admins, write_admins


def save_env(path: Path, values: dict[str, str]) -> None:
    """Atomic replacement, mode 0600, preserving unknown settings and owner."""
    path = path.absolute()
    if path.is_symlink():
        raise ValueError("فایل تنظیمات نباید symbolic link باشد.")
    previous = path.stat() if path.exists() else None
    merged = {**read_env(path), **values}
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=".env-", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(descriptor, 0o600)
        if previous and os.geteuid() == 0:
            os.fchown(descriptor, previous.st_uid, previous.st_gid)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("# Private configuration. Never commit this file. Do not source in a shell.\n")
            for key, value in merged.items():
                stream.write(f"{key}={json.dumps(str(value), ensure_ascii=False)}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def persist_settings(env_file: Path, values: dict[str, str]):
    """Validate once, then save both private configuration files."""
    env_file = env_file.absolute()
    settings = settings_from_values(values, base_dir=env_file.parent)
    save_env(env_file, values)
    write_admins(env_file.parent / "admins.json", settings.admin_ids)
    return settings


def ask(values: dict[str, str], key: str, label: str, *, secret: bool = False, optional: bool = False) -> None:
    current = values.get(key, DEFAULTS.get(key, ""))
    if secret:
        suffix = " [تنظیم شده]" if current else " [خالی]"
    else:
        shown = current.replace("\n", "\\n")
        suffix = f" [{shown}]" if shown else " [خالی]"
    prompt = label + suffix + ": "
    answer = getpass.getpass(prompt) if secret else input(prompt)
    if answer == "":
        values[key] = current
    elif optional and answer.strip() == "-":
        values[key] = ""
    else:
        if key in {"WELCOME_TEXT", "DELIVERY_TEMPLATE"}:
            values[key] = answer.replace("\\n", "\n")
        else:
            values[key] = answer if key == "XUI_PASSWORD" else answer.strip()


def identity(values: dict[str, str]) -> None:
    ask(values, "BOT_TOKEN", "توکن BotFather", secret=True)
    ask(values, "ADMIN_IDS", "شناسه عددی ادمین‌ها (با ویرگول)")


def branding(values: dict[str, str]) -> None:
    ask(values, "BRAND_NAME", "نام برند")
    print("متغیر خوش‌آمدگویی: {brand} و {name}. برای خط جدید از \\n استفاده کنید.")
    ask(values, "WELCOME_TEXT", "متن خوش‌آمدگویی")
    print("متغیر تحویل: {brand}, {name}, {plan}, {order_id}, {expires_at}")
    ask(values, "DELIVERY_TEMPLATE", "متن آماده تحویل کانفیگ")


def payment(values: dict[str, str]) -> None:
    ask(values, "CARD_NUMBER", "شماره کارت ۱۶ رقمی", secret=True, optional=True)
    ask(values, "CARD_HOLDER", "نام صاحب کارت", optional=True)
    print("چرخش چند کارت اختیاری: JSON مثل [{\"number\":\"0000000000000000\",\"holder\":\"نام\"}]؛ [] یعنی کارت اصلی")
    ask(values, "CARDS_JSON", "لیست کارت‌های چرخشی", secret=True)
    ask(values, "SUPPORT_USERNAME", "آیدی پشتیبانی یا لینک t.me", optional=True)
    ask(values, "CHANNEL_USERNAME", "آیدی یا لینک کانال (اختیاری)", optional=True)


def panel(values: dict[str, str]) -> None:
    print("۱) بدون پنل: تحویل دستی توسط ادمین یا موجودی آماده  ۲) اتصال 3x-ui  Enter) حفظ تنظیم فعلی")
    choice = input("روش ارائه سرویس: ").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    if choice == "1":
        values["PANEL_CONNECTED"] = "false"
        values.update({key: "" for key in ("XUI_URL", "XUI_USERNAME", "XUI_PASSWORD", "XUI_SUB_URL", "XUI_API_TOKEN")})
        return
    if choice not in {"", "2"}:
        print("گزینه معتبر نیست؛ تنظیم پنل تغییر نکرد.")
        return
    if choice == "":
        return
    if choice == "2":
        values["PANEL_CONNECTED"] = "true"
    print("آدرس کامل پنل با مسیر مخفی را وارد کنید. شناسه inbound برای هر پلن در پنل ادمین ربات تنظیم می‌شود.")
    ask(values, "XUI_URL", "آدرس پنل مانند https://panel.example.com/secret-path", secret=True)
    ask(values,"XUI_API_TOKEN","API Token پنل (اختیاری؛ جایگزین نام کاربری و رمز)",secret=True,optional=True)
    ask(values, "XUI_USERNAME", "نام کاربری پنل", secret=True,optional=True)
    ask(values, "XUI_PASSWORD", "رمز پنل", secret=True,optional=True)
    ask(values, "XUI_SUB_URL", "آدرس پایه سابسکریپشن مانند https://sub.example.com/sub", secret=True)
    ask(values, "XUI_VERIFY_TLS", "بررسی گواهی TLS (true پیشنهادی / false)")


def build_proxy_url(scheme: str, host: str, port: str, username: str = "", password: str = "") -> str:
    scheme = scheme.lower()
    host = host.strip().strip("[]")
    port = port.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")).strip()
    if scheme not in {"http", "https", "socks5", "socks5h"}:
        raise ValueError("پروتکل پراکسی معتبر نیست.")
    if not host or any(c.isspace() for c in host) or any(c in host for c in "/?#@"):
        raise ValueError("IP یا دامنه پراکسی معتبر نیست.")
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("پورت پراکسی باید بین ۱ تا ۶۵۵۳۵ باشد.")
    shown_host = f"[{host}]" if ":" in host else host
    auth = ""
    if username:
        auth = quote(username, safe="") + ":" + quote(password, safe="") + "@"
    return f"{scheme}://{auth}{shown_host}:{int(port)}"


def network(values: dict[str, str]) -> None:
    print("پراکسی فقط برای اتصال ربات به تلگرام است.")
    print("۱) بدون پراکسی  ۲) HTTP  ۳) HTTPS  ۴) SOCKS5  ۵) SOCKS5H  ۶) وارد کردن آدرس کامل")
    choice = input("پروتکل [Enter: حفظ مقدار فعلی]: ").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    if not choice:
        return
    if choice == "1":
        values["TELEGRAM_PROXY_URL"] = ""
        return
    if choice == "6":
        ask(values, "TELEGRAM_PROXY_URL", "آدرس کامل پراکسی", secret=True, optional=True)
        return
    schemes = {"2": "http", "3": "https", "4": "socks5", "5": "socks5h"}
    if choice not in schemes:
        raise ValueError("یکی از گزینه‌های ۱ تا ۶ را انتخاب کنید.")
    host = input("IP یا دامنه پراکسی: ").strip()
    port = input("پورت: ").strip()
    username = input("نام کاربری پراکسی [اختیاری]: ").strip()
    password = getpass.getpass("رمز پراکسی [اختیاری]: ") if username else ""
    values["TELEGRAM_PROXY_URL"] = build_proxy_url(schemes[choice], host, port, username, password)
    print(f"پراکسی {schemes[choice].upper()} تنظیم شد؛ آدرس و رمز نمایش داده نمی‌شوند.")


def main() -> int:
    parser = argparse.ArgumentParser(description="تنظیم امن ربات از SSH؛ بدون نیاز به کتابخانه خارجی")
    parser.add_argument("--env-file", type=Path, default=ENV_FILE)
    parser.add_argument("--check", action="store_true", help="بررسی آفلاین تنظیمات؛ بدون چاپ رمز")
    args = parser.parse_args()
    try:
        values = {**DEFAULTS, **read_env(args.env_file)}
        admin_file = args.env_file.parent / "admins.json"
        if admin_file.exists():
            existing_ids = read_admins(admin_file)
            if existing_ids:
                values["ADMIN_IDS"] = ",".join(map(str, existing_ids))
        if args.check:
            if not admin_file.exists() or not read_admins(admin_file):
                raise ValueError("فایل admins.json خالی یا ناموجود است؛ setup.py را اجرا کنید.")
            settings_from_values(values, base_dir=args.env_file.absolute().parent)
            print("تنظیمات از نظر ساختار معتبر است؛ اتصال تلگرام و پنل در این بررسی تست نشده است.")
            return 0
        print("تنظیم ربات | Enter: حفظ مقدار قبلی | -: حذف مقدار اختیاری")
        print("اطلاعات حساس هنگام ورود نمایش داده نمی‌شوند. نصب و تنظیمات عادی داده‌ها را پاک نمی‌کنند.")
        if not args.env_file.exists():
            print("\nراه‌اندازی سریع شروع شد؛ بعد از آخرین سؤال، تنظیمات خودکار ذخیره می‌شوند.")
            for action in (identity, branding, payment, panel, network):
                action(values)
            try:
                persist_settings(args.env_file, values)
            except ValueError as exc:
                print(f"\nذخیره انجام نشد: {exc}")
                print("بخش مربوط را از منوی زیر اصلاح کن؛ هر بخش بعد از ویرایش خودکار ذخیره می‌شود.")
            else:
                print("\n✅ راه‌اندازی کامل شد و تنظیمات امن ذخیره شدند.")
                print("حالا ربات را با bash run.sh اجرا کن؛ در نصب systemd سرویس به‌صورت خودکار شروع می‌شود.")
                return 0
        actions = {"1": identity, "2": branding, "3": payment, "4": panel, "5": network}
        while True:
            print("\nهر بخشی را تغییر بده؛ همان بخش بلافاصله ذخیره می‌شود.")
            print("۱) توکن و ادمین  ۲) برند و متن‌ها  ۳) پرداخت و پشتیبانی  ۴) پنل  ۵) پروکسی  ۶) بررسی و خروج  ۰) خروج")
            choice = input("انتخاب [6]: ").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")) or "6"
            if choice == "0":
                print("خارج شدی؛ تغییرات موفق قبلی ذخیره شده‌اند.")
                return 0
            if choice in actions:
                before = dict(values)
                actions[choice](values)
                try:
                    persist_settings(args.env_file, values)
                except (OSError, ValueError) as exc:
                    values = before
                    print(f"❌ ذخیره نشد: {exc}")
                else:
                    print("✅ این بخش ذخیره شد. برای اعمال توکن، پنل یا پروکسی روی ربات فعال، سرویس را restart کن.")
            elif choice == "6":
                try:
                    persist_settings(args.env_file, values)
                except (OSError, ValueError) as exc:
                    print(f"❌ بررسی ناموفق بود: {exc}")
                    continue
                print("✅ تنظیمات سالم و ذخیره‌شده‌اند.")
                print("اگر تنظیمات SSH را تغییر دادی، ربات در حال اجرا را restart کن.")
                return 0
            else:
                print("یکی از گزینه‌های ۰ تا ۶ را انتخاب کنید.")
    except (EOFError, KeyboardInterrupt):
        print("\nلغو شد؛ تغییری ذخیره نشد.")
        return 130
    except (OSError, ValueError) as exc:
        print(f"خطا در تنظیمات: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
