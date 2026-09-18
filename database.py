"""SQLite storage. All balance, inventory and order changes are transactional.

Run one bot process per database. Startup recovery requires the process lock acquired
by bot.py before init(). Network calls never run inside a database transaction.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
from typing import Any
from urllib.parse import urlsplit
import uuid


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


PROTOCOLS = ("v2ray", "wireguard", "openvpn")


def validate_config(value: str, protocol: str = "v2ray") -> str:
    value = str(value).strip()
    if protocol not in PROTOCOLS:
        raise ValueError("پروتکل نامعتبر است.")
    if not value or len(value) > 50_000:
        raise ValueError("کانفیگ خالی است یا بیش از حد مجاز طول دارد.")
    if protocol == "wireguard":
        if "[Interface]" not in value or "[Peer]" not in value:
            raise ValueError("کانفیگ WireGuard باید بخش‌های [Interface] و [Peer] را داشته باشد.")
        return value
    if protocol == "openvpn":
        if not any(line.strip().lower() == "client" for line in value.splitlines()):
            raise ValueError("متن OpenVPN باید یک کانفیگ client معتبر باشد.")
        return value
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError("برای هر سرویس فقط یک لینک اشتراک بفرستید.")
    for line in lines:
        if any(char.isspace() for char in line) or not line.startswith(("https://", "http://")):
            raise ValueError("فقط لینک سابسکریپشن کامل با http یا https قابل قبول است.")
        try:
            parsed = urlsplit(line)
        except ValueError:
            raise ValueError("فرمت لینک اشتراک معتبر نیست.") from None
        if not parsed.netloc:
            raise ValueError("لینک اشتراک کامل نیست.")
    return "\n".join(lines)


SCHEMA = """
CREATE TABLE IF NOT EXISTS shop_users (
 id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '', full_name TEXT NOT NULL DEFAULT '',
 balance INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0), blocked INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shop_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shop_plans (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, months INTEGER NOT NULL CHECK(months IN (1,2)),
 days INTEGER NOT NULL, traffic_gb REAL NOT NULL CHECK(traffic_gb >= 0), price INTEGER NOT NULL CHECK(price > 0),
 mode TEXT NOT NULL CHECK(mode IN ('manual','inventory','xui')), inbound_id INTEGER NOT NULL DEFAULT 0,
 active INTEGER NOT NULL DEFAULT 1, description TEXT NOT NULL DEFAULT '',
 location_id TEXT NOT NULL DEFAULT '', protocol TEXT NOT NULL DEFAULT 'v2ray', legacy_id TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS shop_orders (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES shop_users(id),
 kind TEXT NOT NULL CHECK(kind IN ('purchase','topup')), method TEXT NOT NULL CHECK(method IN ('card','wallet')),
 plan_id INTEGER REFERENCES shop_plans(id), amount INTEGER NOT NULL CHECK(amount >= 0),
 status TEXT NOT NULL, receipt_file_id TEXT NOT NULL DEFAULT '', plan_snapshot TEXT NOT NULL DEFAULT '{}',
 request_key TEXT, remote_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 error TEXT NOT NULL DEFAULT '', delivered_at TEXT, wallet_debited INTEGER NOT NULL DEFAULT 0,
 approved_at TEXT, card_snapshot TEXT NOT NULL DEFAULT '{}', remote_uncertain INTEGER NOT NULL DEFAULT 0, UNIQUE(user_id,request_key)
);
CREATE TABLE IF NOT EXISTS shop_inventory (
 id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES shop_plans(id),
 config_text TEXT NOT NULL UNIQUE, order_id INTEGER UNIQUE REFERENCES shop_orders(id), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shop_services (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES shop_users(id),
 order_id INTEGER NOT NULL UNIQUE REFERENCES shop_orders(id), plan_name TEXT NOT NULL,
 months INTEGER NOT NULL, traffic_gb REAL NOT NULL, config_text TEXT NOT NULL,
 remote_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, expires_at TEXT,
 document_file_id TEXT NOT NULL DEFAULT '', document_name TEXT NOT NULL DEFAULT '', protocol TEXT NOT NULL DEFAULT 'v2ray'
);
CREATE TABLE IF NOT EXISTS shop_wallet_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES shop_users(id),
 order_id INTEGER NOT NULL REFERENCES shop_orders(id), delta INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shop_locations (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'country',
 active INTEGER NOT NULL DEFAULT 1, archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS shop_location_protocols (
 location_id TEXT NOT NULL REFERENCES shop_locations(id), protocol TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(location_id,protocol)
);
CREATE TABLE IF NOT EXISTS card_rotation (id INTEGER PRIMARY KEY CHECK(id=1), current_index INTEGER NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO card_rotation(id,current_index) VALUES(1,0);
CREATE INDEX IF NOT EXISTS shop_orders_user_idx ON shop_orders(user_id,id DESC);
CREATE INDEX IF NOT EXISTS shop_orders_status_idx ON shop_orders(status,id);
CREATE INDEX IF NOT EXISTS shop_inventory_free_idx ON shop_inventory(plan_id,order_id);
CREATE INDEX IF NOT EXISTS shop_services_user_idx ON shop_services(user_id,id DESC);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=20000")
        return conn

    async def _run(self, operation, *, write=False):
        # The bot owns an exclusive process lock for this database. Keeping these
        # short SQLite transactions on the event-loop thread also avoids the
        # broken default-executor shutdown present in some Python 3.14 builds.
        conn = self._connection()
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            result = operation(conn)
            if write:
                conn.commit()
            return result
        except BaseException:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    async def init(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        def initialize(conn):
            conn.execute("PRAGMA journal_mode=WAL")
            # Preserve a consistent original before the first additive legacy import.
            legacy = conn.execute("SELECT 1 FROM sqlite_master WHERE name='users' AND type='table'").fetchone()
            extended = conn.execute("SELECT 1 FROM sqlite_master WHERE name='shop_users' AND type='table'").fetchone()
            if legacy and not extended:
                backup_path = self.path.with_suffix(self.path.suffix+".before-upgrade.bak")
                if not backup_path.exists():
                    backup = sqlite3.connect(backup_path)
                    try:
                        conn.backup(backup)
                    finally:
                        backup.close()
                    backup_path.chmod(0o600)
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(shop_locations)")}
            if "kind" not in columns:
                conn.execute("ALTER TABLE shop_locations ADD COLUMN kind TEXT NOT NULL DEFAULT 'country'")
                conn.execute("UPDATE shop_locations SET kind='multi' WHERE id='multi'")
            # Existing installations used V2Ray for every old location. Keep those
            # locations usable while fresh databases intentionally start empty.
            conn.execute("INSERT OR IGNORE INTO shop_location_protocols(location_id,protocol,active) SELECT id,'v2ray',1 FROM shop_locations")
            from legacy import migrate_legacy
            migrate_legacy(conn)
            # Only called while bot.py holds the exclusive application lock.
            conn.execute("UPDATE shop_orders SET status='failed',remote_uncertain=1,error=?,updated_at=? WHERE status='processing'",
                         ("عملیات با توقف ربات نیمه‌تمام ماند؛ مدیر باید دوباره تلاش کند.", utcnow()))
        await self._run(initialize)
        self.path.chmod(0o600)

    async def close(self):
        # Each worker closes its own connection, so there is no shared connection.
        return None

    @staticmethod
    def _row(row) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        if "card_snapshot" in result:
            result["card_snapshot"] = json.loads(result["card_snapshot"])
        if "plan_snapshot" in result:
            result["plan_snapshot"] = json.loads(result["plan_snapshot"])
        for field in ("blocked", "active", "wallet_debited"):
            if field in result:
                result[field] = bool(result[field])
        return result

    @classmethod
    def _order(cls, conn, order_id):
        row = cls._row(conn.execute("SELECT * FROM shop_orders WHERE id=?", (order_id,)).fetchone())
        if row is None:
            raise ValueError("سفارش پیدا نشد.")
        return row

    @classmethod
    def _user(cls, conn, user_id, *, allow_blocked=False):
        row = cls._row(conn.execute("SELECT * FROM shop_users WHERE id=?", (user_id,)).fetchone())
        if row is None:
            raise ValueError("ابتدا ربات را با /start راه‌اندازی کنید.")
        if row["blocked"] and not allow_blocked:
            raise ValueError("حساب شما غیرفعال است؛ با پشتیبانی تماس بگیرید.")
        return row

    async def get_user(self, user_id):
        return await self._run(lambda c: self._row(c.execute("SELECT * FROM shop_users WHERE id=?", (user_id,)).fetchone()))

    async def upsert_user(self, user_id, username="", full_name=""):
        def update(conn):
            conn.execute("INSERT INTO shop_users(id,username,full_name,created_at) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username,full_name=excluded.full_name",
                         (user_id, str(username or "")[:64], str(full_name or "")[:256], utcnow()))
            return self._user(conn, user_id, allow_blocked=True)
        return await self._run(update, write=True)

    async def list_users(self, limit=20, offset=0):
        return await self._run(lambda c: [self._row(r) for r in c.execute("SELECT * FROM shop_users ORDER BY id DESC LIMIT ? OFFSET ?", (min(max(int(limit), 1), 100), max(int(offset), 0)))])

    async def set_user_blocked(self, user_id, blocked):
        await self._run(lambda c: c.execute("UPDATE shop_users SET blocked=? WHERE id=?", (int(bool(blocked)), user_id)).rowcount, write=True)

    async def get_settings(self):
        return await self._run(lambda c: dict(c.execute("SELECT key,value FROM shop_settings")))

    async def set_setting(self, key, value):
        if not isinstance(key, str) or len(key) > 100 or len(str(value)) > 16000:
            raise ValueError("تنظیمات معتبر نیست.")
        await self._run(lambda c: c.execute("INSERT INTO shop_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value))).rowcount, write=True)

    async def list_plans(self, months=None, active_only=True, location_id=None, protocol=None):
        clauses, values = [], []
        if months is not None:
            clauses.append("months=?")
            values.append(int(months))
        if active_only:
            clauses.append("active=1")
        for field, value in (("location_id",location_id),("protocol",protocol)):
            if value is not None:
                clauses.append(field+"=?")
                values.append(value)
        query = "SELECT * FROM shop_plans" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY months,price,id"
        return await self._run(lambda c: [self._row(r) for r in c.execute(query, values)])

    async def get_plan(self, plan_id):
        return await self._run(lambda c: self._row(c.execute("SELECT * FROM shop_plans WHERE id=?", (plan_id,)).fetchone()))

    async def save_plan(self, plan):
        def save(conn):
            old = self._row(conn.execute("SELECT * FROM shop_plans WHERE id=?", (plan.get("id"),)).fetchone()) if plan.get("id") else None
            if plan.get("id") and old is None:
                raise ValueError("پلن پیدا نشد.")
            value = dict(old or {})
            value.update(plan)
            try:
                months = int(value["months"])
                price = int(value["price"])
                traffic = float(value.get("traffic_gb", 0))
                inbound = int(value.get("inbound_id", 0))
            except (KeyError, TypeError, ValueError, OverflowError):
                raise ValueError("مدت، قیمت، حجم یا شماره اینباند معتبر نیست.") from None
            mode = value.get("mode", "manual")
            name = str(value.get("name", "")).strip()
            if months not in (1, 2) or not 0 < price <= 10**12 or not math.isfinite(traffic) or not 0 <= traffic <= 10**7:
                raise ValueError("مدت باید ۱ یا ۲ ماه، قیمت مثبت و حجم نامنفی باشد.")
            if not name or len(name) > 120 or mode not in ("manual", "inventory", "xui") or inbound < 0:
                raise ValueError("نام پلن یا روش تحویل معتبر نیست.")
            if mode == "xui" and inbound <= 0:
                raise ValueError("برای تحویل با پنل، شماره اینباند را وارد کنید.")
            location_id = str(value.get("location_id", ""))
            protocol = str(value.get("protocol", "v2ray"))
            if not conn.execute("SELECT 1 FROM shop_locations WHERE id=? AND archived=0", (location_id,)).fetchone():
                raise ValueError("لوکیشن پلن معتبر نیست.")
            enabled = conn.execute("SELECT active FROM shop_location_protocols WHERE location_id=? AND protocol=?", (location_id, protocol)).fetchone()
            if protocol not in PROTOCOLS or not enabled or not enabled[0]:
                raise ValueError("پروتکل انتخابی برای این لوکیشن روشن نیست.")
            if mode == "xui" and protocol != "v2ray":
                raise ValueError("ساخت خودکار پنل فعلی فقط برای کانفیگ عادی است.")
            description = str(value.get("description", ""))
            if len(description) > 2000:
                raise ValueError("توضیح پلن بیش از حد طولانی است.")
            params = (name, months, months * 30, traffic, price, mode, inbound, int(bool(value.get("active", True))), description)
            if old:
                conn.execute("UPDATE shop_plans SET name=?,months=?,days=?,traffic_gb=?,price=?,mode=?,inbound_id=?,active=?,description=? WHERE id=?", (*params, old["id"]))
                plan_id = old["id"]
            else:
                plan_id = conn.execute("INSERT INTO shop_plans(name,months,days,traffic_gb,price,mode,inbound_id,active,description) VALUES(?,?,?,?,?,?,?,?,?)", params).lastrowid
            conn.execute("UPDATE shop_plans SET location_id=?,protocol=?,legacy_id=? WHERE id=?", (location_id, protocol, value.get("legacy_id"), plan_id))
            return self._row(conn.execute("SELECT * FROM shop_plans WHERE id=?", (plan_id,)).fetchone())
        return await self._run(save, write=True)

    async def archive_plan(self, plan_id):
        await self._run(lambda c: c.execute("UPDATE shop_plans SET active=0 WHERE id=?", (plan_id,)).rowcount, write=True)

    async def add_inventory(self, plan_id, configs: list[str]):
        def add(conn):
            plan = conn.execute("SELECT id,protocol FROM shop_plans WHERE id=?", (plan_id,)).fetchone()
            if plan is None:
                raise ValueError("پلن پیدا نشد.")
            values = list(dict.fromkeys(validate_config(value, plan["protocol"]) for value in configs))
            if not values or len(values) > 10000:
                raise ValueError("بین ۱ تا ۱۰۰۰۰ کانفیگ وارد کنید.")
            count = 0
            for value in values:
                count += conn.execute("INSERT OR IGNORE INTO shop_inventory(plan_id,config_text,created_at) VALUES(?,?,?)", (plan_id, value, utcnow())).rowcount
            return count
        return await self._run(add, write=True)

    async def inventory_count(self, plan_id):
        return await self._run(lambda c: c.execute("SELECT COUNT(*) FROM shop_inventory WHERE plan_id=? AND order_id IS NULL", (plan_id,)).fetchone()[0])

    async def get_stats(self):
        def stats(conn):
            result = {key: conn.execute(f"SELECT COUNT(*) FROM shop_{key}").fetchone()[0] for key in ("users", "orders", "services")}
            result["pending_orders"] = conn.execute("SELECT COUNT(*) FROM shop_orders WHERE status IN ('awaiting_review','processing','failed','awaiting_config','legacy_review')").fetchone()[0]
            result["revenue"] = conn.execute("SELECT COALESCE(SUM(amount),0) FROM shop_orders WHERE kind='purchase' AND status='paid'").fetchone()[0]
            result["balance_total"] = conn.execute("SELECT COALESCE(SUM(balance),0) FROM shop_users").fetchone()[0]
            return result
        return await self._run(stats)

    async def create_order(self, user_id, plan_id=None, amount=None, kind="purchase", method="card", request_key=None, expected_price=None):
        if kind not in ("purchase", "topup") or method not in ("card", "wallet") or (kind == "topup" and method != "card"):
            raise ValueError("نوع سفارش یا روش پرداخت معتبر نیست.")
        if request_key is not None and (not str(request_key) or len(str(request_key)) > 250):
            raise ValueError("شناسه درخواست معتبر نیست.")
        def create(conn):
            self._user(conn, user_id)
            if request_key is not None:
                previous = self._row(conn.execute("SELECT * FROM shop_orders WHERE user_id=? AND request_key=?", (user_id, str(request_key))).fetchone())
                if previous:
                    if previous["kind"] != kind or previous["method"] != method or previous["plan_id"] != plan_id or (kind == "topup" and previous["amount"] != amount):
                        raise ValueError("این درخواست قبلاً با مشخصات دیگری ثبت شده است.")
                    return previous
            snapshot = {}
            if kind == "purchase":
                snapshot = self._row(conn.execute("SELECT * FROM shop_plans WHERE id=? AND active=1", (plan_id,)).fetchone())
                if snapshot is None:
                    raise ValueError("این پلن در حال حاضر قابل خرید نیست.")
                location = conn.execute("SELECT active,archived FROM shop_locations WHERE id=?", (snapshot["location_id"],)).fetchone()
                protocol = conn.execute("SELECT active FROM shop_location_protocols WHERE location_id=? AND protocol=?", (snapshot["location_id"], snapshot["protocol"])).fetchone()
                if not location or not location["active"] or location["archived"] or not protocol or not protocol["active"]:
                    raise ValueError("این پلن فعلاً فعال نیست.")
                charge = snapshot["price"]
                if expected_price is not None and charge != expected_price:
                    raise ValueError("قیمت پلن تغییر کرده؛ دوباره پلن را باز کنید و مبلغ جدید را ببینید.")
            else:
                if plan_id is not None:
                    raise ValueError("شارژ کیف پول به پلن وابسته نیست.")
                try:
                    charge = int(amount)
                except (TypeError, ValueError, OverflowError):
                    raise ValueError("مبلغ شارژ معتبر نیست.") from None
                if charge != amount or not 0 < charge <= 10**12:
                    raise ValueError("مبلغ شارژ باید عدد صحیح و مثبت باشد.")
            now = utcnow()
            order_id = conn.execute("INSERT INTO shop_orders(user_id,kind,method,plan_id,amount,status,plan_snapshot,request_key,remote_key,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?,?,?,?)",
                                    (user_id, kind, method, plan_id, charge, json.dumps(snapshot, ensure_ascii=False), str(request_key) if request_key is not None else None, uuid.uuid4().hex, now, now)).lastrowid
            return self._order(conn, order_id)
        return await self._run(create, write=True)

    async def get_order(self, order_id):
        return await self._run(lambda c: self._row(c.execute("SELECT * FROM shop_orders WHERE id=?", (order_id,)).fetchone()))

    async def user_orders(self, user_id, limit=20):
        return await self._run(lambda c: [self._row(r) for r in c.execute("SELECT * FROM shop_orders WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, min(max(int(limit), 1), 100)))])

    async def pending_orders(self, limit=20, offset=0):
        return await self._run(lambda c: [self._row(r) for r in c.execute("SELECT * FROM shop_orders WHERE status IN ('awaiting_review','awaiting_config','failed','processing','legacy_review') OR (status='paid' AND delivered_at IS NULL) ORDER BY id LIMIT ? OFFSET ?", (min(max(int(limit), 1), 100), max(int(offset), 0)))])

    async def attach_receipt(self, order_id, user_id, file_id):
        if not file_id or len(str(file_id)) > 1000:
            raise ValueError("رسید معتبر نیست.")
        def attach(conn):
            self._user(conn, user_id)
            return bool(conn.execute("UPDATE shop_orders SET receipt_file_id=?,status='awaiting_review',updated_at=? WHERE id=? AND user_id=? AND method='card' AND status IN ('pending','awaiting_review')", (str(file_id), utcnow(), order_id, user_id)).rowcount)
        return await self._run(attach, write=True)

    async def cancel_order(self, order_id, user_id):
        # Users can only cancel unpaid orders, never interrupt provisioning.
        return await self._run(lambda c: bool(c.execute("UPDATE shop_orders SET status='cancelled',updated_at=? WHERE id=? AND user_id=? AND status='pending' AND wallet_debited=0", (utcnow(), order_id, user_id)).rowcount), write=True)

    @staticmethod
    def _wallet(conn, order, delta, reason):
        if delta < 0:
            changed = conn.execute("UPDATE shop_users SET balance=balance+? WHERE id=? AND balance>=?", (delta, order["user_id"], -delta)).rowcount
            if not changed:
                raise ValueError("موجودی کیف پول کافی نیست.")
        else:
            conn.execute("UPDATE shop_users SET balance=balance+? WHERE id=?", (delta, order["user_id"]))
        conn.execute("INSERT INTO shop_wallet_events(user_id,order_id,delta,reason,created_at) VALUES(?,?,?,?,?)", (order["user_id"], order["id"], delta, reason, utcnow()))

    async def reject_order(self, order_id):
        def reject(conn):
            order = self._order(conn, order_id)
            if order["status"] in ("rejected", "cancelled"):
                return False
            if order["status"] in ("paid", "processing"):
                return False
            # A failed remote call with held funds may already have made a client.
            if order["status"] == "failed" and (order["wallet_debited"] or order["remote_uncertain"]):
                raise ValueError("نتیجه ساخت سرویس نامشخص است؛ ابتدا دوباره تلاش کنید.")
            if order["status"] == "awaiting_config" and order["method"] == "card":
                raise ValueError("پرداخت این سفارش تأیید شده؛ برای جلوگیری از حذف وجه، سرویس را تحویل دهید.")
            if order["wallet_debited"]:
                self._wallet(conn, order, order["amount"], "refund")
            conn.execute("UPDATE shop_orders SET status='rejected',wallet_debited=0,updated_at=? WHERE id=?", (utcnow(), order_id))
            conn.execute("UPDATE shop_inventory SET order_id=NULL WHERE order_id=?", (order_id,))
            return True
        return await self._run(reject, write=True)

    async def approve_topup(self, order_id):
        def approve(conn):
            order = self._order(conn, order_id)
            if order["kind"] != "topup" or order["method"] != "card":
                raise ValueError("این سفارش شارژ کیف پول نیست.")
            if order["status"] == "paid":
                return order
            if order["status"] != "awaiting_review" or not order["receipt_file_id"]:
                raise ValueError("این سفارش رسید قابل تأیید ندارد.")
            self._wallet(conn, order, order["amount"], "topup")
            now = utcnow()
            conn.execute("UPDATE shop_orders SET status='paid',approved_at=?,updated_at=? WHERE id=?", (now, now, order_id))
            return self._order(conn, order_id)
        return await self._run(approve, write=True)

    @staticmethod
    def _reserve(conn, order):
        existing = conn.execute("SELECT config_text FROM shop_inventory WHERE order_id=?", (order["id"],)).fetchone()
        if existing:
            return existing[0]
        row = conn.execute("SELECT id,config_text FROM shop_inventory WHERE plan_id=? AND order_id IS NULL ORDER BY id LIMIT 1", (order["plan_id"],)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE shop_inventory SET order_id=? WHERE id=?", (order["id"], row["id"]))
        return row["config_text"]

    def _claim(self, conn, order_id, from_wallet, *, manual=False):
        order = self._order(conn, order_id)
        if order["kind"] != "purchase":
            raise ValueError("این سفارش خرید سرویس نیست.")
        if bool(from_wallet) != (order["method"] == "wallet"):
            raise ValueError("روش پرداخت سفارش با این عملیات سازگار نیست.")
        if order["status"] in ("paid", "processing", "awaiting_config"):
            return None
        self._user(conn, order["user_id"])
        if from_wallet:
            allowed = ("pending", "failed")
        else:
            allowed = ("awaiting_review", "failed")
            if not order["receipt_file_id"]:
                raise ValueError("ابتدا رسید پرداخت را ارسال کنید.")
        if order["status"] not in allowed:
            raise ValueError("این سفارش دیگر قابل تأیید نیست.")
        mode = order["plan_snapshot"]["mode"]
        if manual != (mode == "manual"):
            raise ValueError("روش تحویل سفارش معتبر نیست.")
        if mode == "inventory" and self._reserve(conn, order) is None:
            raise ValueError("موجودی کانفیگ این پلن تمام شده؛ مبلغی از کیف پول کم نشد.")
        if from_wallet and not order["wallet_debited"]:
            self._wallet(conn, order, -order["amount"], "purchase")
        now = utcnow()
        conn.execute("UPDATE shop_orders SET status=?,wallet_debited=?,error='',approved_at=COALESCE(approved_at,?),updated_at=? WHERE id=?", ("awaiting_config" if manual else "processing", int(from_wallet), now, now, order_id))
        return self._order(conn, order_id)

    async def claim_order(self, order_id, from_wallet=False):
        return await self._run(lambda c: self._claim(c, order_id, from_wallet), write=True)

    async def approve_manual_order(self, order_id, from_wallet=False):
        def approve(conn):
            order = self._order(conn, order_id)
            if order["kind"] != "purchase" or order["plan_snapshot"].get("mode") != "manual":
                raise ValueError("این سفارش تحویل دستی ندارد.")
            if order["status"] in ("awaiting_config", "paid"):
                return order
            return self._claim(conn, order_id, from_wallet, manual=True)
        return await self._run(approve, write=True)

    async def reserve_inventory(self, order_id):
        def reserve(conn):
            order = self._order(conn, order_id)
            if order["status"] not in ("processing", "paid") or order["plan_snapshot"].get("mode") != "inventory":
                raise ValueError("این سفارش قابل تخصیص موجودی نیست.")
            return self._reserve(conn, order)
        return await self._run(reserve, write=True)

    def _complete(self, conn, order_id, config_text, remote_id, expires_at, *, manual=False, document_file_id="", document_name=""):
        order = self._order(conn, order_id)
        existing = self._row(conn.execute("SELECT * FROM shop_services WHERE order_id=?", (order_id,)).fetchone())
        if order["status"] == "paid" and existing:
            return existing
        expected = "awaiting_config" if manual else "processing"
        if order["kind"] != "purchase" or order["status"] != expected:
            raise ValueError("سفارش برای تحویل آماده نیست.")
        if order["method"] == "wallet" and not order["wallet_debited"]:
            raise ValueError("پرداخت کیف پول انجام نشده است.")
        plan = order["plan_snapshot"]
        if manual != (plan["mode"] == "manual"):
            raise ValueError("روش تحویل سفارش معتبر نیست.")
        if plan["mode"] == "inventory":
            reserved = conn.execute("SELECT config_text FROM shop_inventory WHERE order_id=?", (order_id,)).fetchone()
            if not reserved or reserved[0] != config_text:
                raise ValueError("کانفیگ با موجودی رزروشده سفارش تطابق ندارد.")
        now = utcnow()
        if expires_at is None:
            expires_at = (datetime.now(timezone.utc) + timedelta(days=plan["days"])).isoformat(timespec="seconds")
        service_id = conn.execute("INSERT INTO shop_services(user_id,order_id,plan_name,months,traffic_gb,config_text,remote_id,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)", (order["user_id"], order_id, plan["name"], plan["months"], plan["traffic_gb"], config_text, str(remote_id), now, expires_at)).lastrowid
        conn.execute("UPDATE shop_services SET document_file_id=?,document_name=?,protocol=? WHERE id=?", (document_file_id,document_name,plan.get("protocol","v2ray"),service_id))
        conn.execute("UPDATE shop_orders SET status='paid',remote_uncertain=0,error='',updated_at=? WHERE id=?", (now, order_id))
        return self._row(conn.execute("SELECT * FROM shop_services WHERE id=?", (service_id,)).fetchone())

    async def complete_order(self, order_id, config_text, remote_id="", expires_at=None):
        order = await self.get_order(order_id)
        config_text = validate_config(config_text, order["plan_snapshot"].get("protocol", "v2ray"))
        return await self._run(lambda c: self._complete(c, order_id, config_text, remote_id, expires_at), write=True)

    async def complete_manual_order(self, order_id, config_text="", *, document_file_id="", document_name=""):
        order = await self.get_order(order_id)
        protocol = order["plan_snapshot"].get("protocol", "v2ray")
        if document_file_id:
            if protocol == "v2ray":
                raise ValueError("برای کانفیگ عادی، لینک سابسکریپشن http/https بفرستید.")
            if len(str(document_file_id)) > 1000 or len(str(document_name)) > 255:
                raise ValueError("فایل کانفیگ معتبر نیست.")
            config_text = ""
        else:
            config_text = validate_config(config_text, protocol)
        return await self._run(lambda c: self._complete(c, order_id, config_text, "", None, manual=True,
            document_file_id=str(document_file_id), document_name=str(document_name)), write=True)

    async def fail_order(self, order_id, error, *, refund=True):
        def fail(conn):
            order = self._order(conn, order_id)
            if order["status"] != "processing":
                return
            if refund and order["wallet_debited"]:
                self._wallet(conn, order, order["amount"], "refund")
            conn.execute("UPDATE shop_orders SET status='failed',error=?,wallet_debited=?,remote_uncertain=?,updated_at=? WHERE id=?", (str(error)[:500], int(order["wallet_debited"] and not refund), int(not refund), utcnow(), order_id))
            # Reservation stays with this order for deterministic retries.
        await self._run(fail, write=True)

    async def get_service(self, service_id, user_id=None):
        query = "SELECT * FROM shop_services WHERE id=?" + (" AND user_id=?" if user_id is not None else "")
        args = (service_id, user_id) if user_id is not None else (service_id,)
        return await self._run(lambda c: self._row(c.execute(query, args).fetchone()))

    async def service_for_order(self, order_id):
        return await self._run(lambda c: self._row(c.execute("SELECT * FROM shop_services WHERE order_id=?", (order_id,)).fetchone()))

    async def list_services(self, user_id, limit=20, offset=0):
        return await self._run(lambda c: [self._row(r) for r in c.execute("SELECT * FROM shop_services WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (user_id, min(max(int(limit), 1), 100), max(int(offset), 0)))])

    async def mark_delivered(self, order_id):
        await self._run(lambda c: c.execute("UPDATE shop_orders SET delivered_at=COALESCE(delivered_at,?) WHERE id=? AND status='paid'", (utcnow(), order_id)).rowcount, write=True)

    async def undelivered_orders(self):
        return await self._run(lambda c: [self._row(r) for r in c.execute("SELECT * FROM shop_orders WHERE status='paid' AND delivered_at IS NULL ORDER BY id LIMIT 100")])


    async def list_locations(self, active_only=False):
        query = "SELECT * FROM shop_locations WHERE archived=0" + (" AND active=1" if active_only else "") + " ORDER BY rowid"
        return await self._run(lambda c: [dict(r) for r in c.execute(query)])

    async def save_location(self, name, location_id=None, active=True, kind="country"):
        name = str(name).strip()
        if not name or len(name)>60:
            raise ValueError("نام لوکیشن باید بین ۱ تا ۶۰ حرف باشد.")
        location_id = location_id or uuid.uuid4().hex[:10]
        if not location_id.replace("_", "").replace("-", "").isalnum() or len(location_id)>30:
            raise ValueError("شناسه لوکیشن نامعتبر است.")
        if kind not in ("country", "multi"):
            raise ValueError("نوع لوکیشن معتبر نیست.")
        await self._run(lambda c: c.execute("INSERT INTO shop_locations(id,name,kind,active) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,active=excluded.active", (location_id,name,kind,int(active))).rowcount, write=True)
        return location_id

    async def location_protocols(self, location_id, active_only=False):
        query = "SELECT protocol,active FROM shop_location_protocols WHERE location_id=?" + (" AND active=1" if active_only else "") + " ORDER BY protocol"
        return await self._run(lambda c: [dict(r) for r in c.execute(query, (location_id,))])

    async def set_location_protocol(self, location_id, protocol, active):
        if protocol not in PROTOCOLS:
            raise ValueError("پروتکل نامعتبر است.")
        def save(conn):
            if not conn.execute("SELECT 1 FROM shop_locations WHERE id=? AND archived=0", (location_id,)).fetchone():
                raise ValueError("لوکیشن پیدا نشد.")
            conn.execute("INSERT INTO shop_location_protocols(location_id,protocol,active) VALUES(?,?,?) ON CONFLICT(location_id,protocol) DO UPDATE SET active=excluded.active", (location_id,protocol,int(bool(active))))
        await self._run(save, write=True)

    async def archive_location(self, location_id):
        await self._run(lambda c: c.execute("UPDATE shop_locations SET archived=1,active=0 WHERE id=?", (location_id,)).rowcount, write=True)

    async def assign_card(self, order_id, cards):
        def assign(conn):
            order = self._order(conn,order_id)
            if order["method"] != "card":
                raise ValueError("این سفارش کارت‌به‌کارت نیست.")
            if order["card_snapshot"]:
                return order["card_snapshot"]
            if not cards:
                raise ValueError("اطلاعات کارت پرداخت هنوز تنظیم نشده است.")
            index = conn.execute("SELECT current_index FROM card_rotation WHERE id=1").fetchone()[0]
            card = cards[index % len(cards)]
            conn.execute("UPDATE card_rotation SET current_index=? WHERE id=1", ((index+1)%len(cards),))
            conn.execute("UPDATE shop_orders SET card_snapshot=? WHERE id=?", (json.dumps(card,ensure_ascii=False),order_id))
            return card
        return await self._run(assign,write=True)

    async def prepare_delivery(self, order_id, panel_connected):
        def prepare(conn):
            order = self._order(conn, order_id)
            if not panel_connected and order["plan_snapshot"].get("mode") == "xui" and order["status"] != "paid":
                if order["remote_uncertain"] or order["status"] == "processing":
                    raise ValueError("ساخت قبلی در پنل نامشخص است؛ برای بررسی دوباره اتصال پنل را روشن کنید.")
                snapshot = order["plan_snapshot"]
                snapshot["mode"] = "manual"
                conn.execute("UPDATE shop_orders SET plan_snapshot=? WHERE id=?", (json.dumps(snapshot,ensure_ascii=False),order_id))
                return self._order(conn,order_id)
            return order
        return await self._run(prepare,write=True)
