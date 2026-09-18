#!/usr/bin/env python3
"""Consistent SQLite backups and explicit reset; never changes bot credentials."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import BASE_DIR, load_settings

CONFIRMATION = "RESET ALL DATA"


@contextmanager
def exclusive_bot_lock(db_path: Path):
    lock_path = db_path.with_suffix(db_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("ربات در حال اجرا است. ابتدا سرویس یا پردازش ربات را متوقف کنید.") from exc
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def backup_database(db_path: Path, destination: Path) -> Path:
    db_path = db_path.resolve()
    if not db_path.is_file():
        raise ValueError("دیتابیسی برای پشتیبان‌گیری وجود ندارد.")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    descriptor, filename = tempfile.mkstemp(prefix=f"bot-{stamp}-", suffix=".sqlite3", dir=destination)
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    target = Path(filename)
    try:
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=30)) as source:
            with closing(sqlite3.connect(target)) as backup:
                source.backup(backup)
                result = backup.execute("PRAGMA quick_check").fetchone()
                if not result or result[0] != "ok":
                    raise ValueError("بررسی سلامت نسخه پشتیبان موفق نبود؛ داده‌ها تغییر نکردند.")
        with target.open("rb") as stream:
            os.fsync(stream.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def reset_database(db_path: Path, destination: Path, confirmation: str) -> Path | None:
    if confirmation != CONFIRMATION:
        raise ValueError("عبارت تأیید درست نیست؛ هیچ داده‌ای پاک نشد.")
    db_path = db_path.resolve()
    with exclusive_bot_lock(db_path):
        if not db_path.exists():
            return None
        backup = backup_database(db_path, destination)
        db_path.unlink()
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        return backup


def main() -> int:
    parser = argparse.ArgumentParser(description="پشتیبان‌گیری امن و ریست صریح دیتابیس")
    parser.add_argument("action", choices=("backup", "reset"))
    parser.add_argument("--db", type=Path, help="مسیر دیتابیس؛ پیش‌فرض از تنظیمات ربات")
    parser.add_argument("--destination", type=Path, default=BASE_DIR / "backups")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        database = args.db or load_settings(require_token=False).db_path
        if args.action == "backup":
            result = backup_database(database, args.destination)
            print(f"نسخه پشتیبان سازگار با SQLite ذخیره شد: {result}")
        else:
            print("این عملیات تمام مشتریان، سفارش‌ها، سرویس‌ها، موجودی، پلن‌ها و تنظیمات پنل ادمین را پاک می‌کند.")
            print("توکن و اطلاعات SSH در .env حفظ می‌شوند. ابتدا ربات را متوقف کنید.")
            print(f"برای ادامه دقیقاً بنویسید: {CONFIRMATION}")
            result = reset_database(database, args.destination, input("تأیید: "))
            if result:
                print(f"ریست انجام شد؛ نسخه پشتیبان قبل از ریست: {result}")
                print("با اجرای بعدی ربات، دیتابیس خالی ساخته می‌شود. پلن‌ها را دوباره بسازید.")
            else:
                print("دیتابیسی وجود نداشت؛ داده‌ای تغییر نکرد.")
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"عملیات انجام نشد: {exc}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nلغو شد.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
